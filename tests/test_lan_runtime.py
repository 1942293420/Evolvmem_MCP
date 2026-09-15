"""LAN runtime isolation and shared-embedding integration tests."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time

import pytest

from evolvmem.config import Config
from evolvmem.context_models import ContextMode
from evolvmem.context_service import ContextService
from evolvmem.lan_config import LanSettings
from evolvmem.lan_runtime import LanRuntime
from evolvmem.mcp_server import MemoryMCPServer


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _settings(tmp_path, *, embedding_enabled=False) -> LanSettings:
    return LanSettings(
        data_dir=tmp_path / "lan-data",
        owner_data_dir=tmp_path / "owner-data",
        token_hashes={
            "jiangli": _digest("jiangli-token"),
            "kane": _digest("kane-token"),
        },
        embedding_enabled=embedding_enabled,
    )


def _add(server, key: str, value: str) -> dict:
    result = server.handle_tool_call("memory_add", {"key": key, "value": value})
    assert result["status"] == "added"
    return result


def _values(server, query: str) -> set[str]:
    result = server.handle_tool_call("memory_search", {"query": query})
    return {row["value"] for row in result["results"]}


def test_lan_settings_authenticate_only_the_two_configured_tokens(tmp_path):
    """A changed digest mapping must not authenticate a missing, wrong, or third token."""
    settings = _settings(tmp_path)

    assert settings.authenticate("jiangli-token") == "jiangli"
    assert settings.authenticate("kane-token") == "kane"
    assert settings.authenticate("") is None
    assert settings.authenticate("wrong-token") is None
    assert settings.authenticate("third-user-token") is None


def test_lan_settings_file_validates_exact_identity_digest_contract(tmp_path):
    """A malformed LAN JSON must fail before it can create an ambiguous namespace."""
    path = tmp_path / "lan.json"
    path.write_text(json.dumps({
        "data_dir": str(tmp_path / "data"),
        "owner_data_dir": str(tmp_path / "owner"),
        "token_hashes": {"jiangli": "bad", "kane": "also-bad"},
    }), encoding="utf-8")

    with pytest.raises(ValueError):
        LanSettings.from_file(path)


def test_runtime_keeps_personal_and_public_mcp_data_isolated_despite_environment(
        tmp_path, monkeypatch):
    """Wrong environment namespaces must never let either user read another space's marker."""
    monkeypatch.setenv("EVOLVMEM_DATA_DIR", str(tmp_path / "hostile-data"))
    monkeypatch.setenv("EVOLVMEM_CONTEXT_MODE", "legacy")
    monkeypatch.setenv("EVOLVMEM_ADAPTER", "hostile")
    settings = _settings(tmp_path)
    runtime = LanRuntime(settings)
    runtime.initialize()
    try:
        jiangli = runtime.server_for("jiangli")
        kane = runtime.server_for("kane")
        public_from_jiangli = runtime.server_for("jiangli", "public")
        public_from_kane = runtime.server_for("kane", "public")

        assert jiangli.config.data_dir == settings.owner_data_dir
        assert kane.config.data_dir == settings.data_dir / "users" / "kane"
        assert public_from_jiangli is public_from_kane
        assert public_from_jiangli.config.data_dir == settings.data_dir / "public"
        assert len({jiangli.config.db_path, kane.config.db_path,
                    public_from_jiangli.config.db_path}) == 3
        for server in (jiangli, kane, public_from_jiangli):
            assert server.config.context_mode == "primary"
            assert server.config.adapter == "codex"
            assert server.config.context_vectors_required is False

        _add(jiangli, "project:lan:fact:jiangli", "jiangli personal marker is private")
        _add(kane, "project:lan:fact:kane", "kane personal marker is private")
        _add(public_from_jiangli, "project:lan:fact:public", "public marker is shared")

        assert "jiangli personal marker is private" in _values(jiangli, "jiangli")
        assert "kane personal marker is private" not in _values(jiangli, "kane")
        assert "kane personal marker is private" in _values(kane, "kane")
        assert "jiangli personal marker is private" not in _values(kane, "jiangli")
        assert "public marker is shared" in _values(public_from_kane, "public")
        assert "public marker is shared" not in _values(jiangli, "public")
    finally:
        runtime.close()


def test_borrowed_missing_embedding_engine_keeps_standalone_fts_usable(tmp_path):
    """Treating an explicit no-model injection as an owned model would break basic writes."""
    server = MemoryMCPServer(config=Config(data_dir=tmp_path), embedding_engine=None)
    server.initialize()
    server._init_done.set()
    try:
        _add(server, "project:lan:fact:fts", "standalone FTS remains available without a model")
        assert "standalone FTS remains available without a model" in _values(
            server, "standalone"
        )
    finally:
        server.shutdown()


def test_runtime_initializes_and_closes_an_injected_shared_engine_once(tmp_path):
    """Creating three MCP namespaces must not create or close one model per namespace."""
    class Engine:
        is_loaded = False

        def __init__(self):
            self.initialize_count = 0
            self.close_count = 0

        def initialize(self):
            self.initialize_count += 1

        def close(self):
            self.close_count += 1

    engine = Engine()
    runtime = LanRuntime(_settings(tmp_path, embedding_enabled=True), engine)
    runtime.initialize()
    runtime.server_for("jiangli")
    runtime.server_for("kane")
    runtime.server_for("kane", "public")
    runtime.close()
    runtime.close()

    assert engine.initialize_count == 1
    assert engine.close_count == 1


def test_runtime_preserves_owner_workspace_key_and_bootstraps_new_namespaces(tmp_path):
    """Replacing the owner's existing key would invalidate existing continuity identities."""
    settings = _settings(tmp_path)
    settings.owner_data_dir.mkdir(parents=True)
    owner_key = settings.owner_data_dir / "workspace.key"
    owner_key.write_bytes(b"o" * 32)
    os.chmod(owner_key, 0o600)

    runtime = LanRuntime(settings)
    runtime.initialize()
    try:
        assert owner_key.read_bytes() == b"o" * 32
        for namespace in (settings.data_dir / "users" / "kane",
                          settings.data_dir / "public"):
            key = namespace / "workspace.key"
            assert key.is_file()
            assert len(key.read_bytes()) == 32
            assert key.stat().st_mode & 0o777 == 0o600
    finally:
        runtime.close()


def test_runtime_serializes_shared_model_encodes_and_uses_its_live_status(tmp_path):
    """A per-server model or unsynchronized encoding would race and report a fake missing file."""
    class Engine:
        def __init__(self):
            self.is_loaded = False
            self.active = 0
            self.maximum_active = 0
            self.lock = threading.Lock()

        def initialize(self):
            self.is_loaded = True

        def encode_query(self, _text):
            with self.lock:
                self.active += 1
                self.maximum_active = max(self.maximum_active, self.active)
            time.sleep(0.01)
            with self.lock:
                self.active -= 1
            return [0.0]

        encode_document = encode_query

        def close(self):
            return None

    engine = Engine()
    runtime = LanRuntime(_settings(tmp_path, embedding_enabled=True), engine)
    runtime.initialize()
    try:
        jiangli = runtime.server_for("jiangli")
        kane = runtime.server_for("kane")
        threads = [threading.Thread(target=server.engine.encode_query, args=("query",))
                   for server in (jiangli, kane)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        status = kane.handle_tool_call("memory_status", {})
        assert status["embedding_loaded"] is True
        assert not any("Model file not found" in item
                       for item in status["embedding_diagnostics"])
        assert engine.maximum_active == 1
    finally:
        runtime.close()


def test_optional_lan_vectors_do_not_hide_database_invariant_failures(tmp_path):
    """Turning vectors optional must not turn a broken Context SQLite schema into ready."""
    runtime = LanRuntime(_settings(tmp_path))
    runtime.initialize()
    try:
        server = runtime.server_for("jiangli")
        service = server.context_service
        assert service.status().ready is True
        connection = sqlite3.connect(server.config.db_path)
        try:
            connection.execute("DROP TABLE context_items")
            connection.commit()
        finally:
            connection.close()

        status = server._live_status()
        assert status is not None
        assert status.mode is ContextMode.PRIMARY
        assert status.ready is False
        assert status.reason_codes == ("degraded_legacy",)
    finally:
        runtime.close()


def test_strict_config_default_still_reports_missing_context_vectors(tmp_path):
    """Changing the default vector policy would silently weaken existing primary gates."""
    config = Config(data_dir=tmp_path)

    assert config.context_vectors_required is True
    service = ContextService(config)
    try:
        status = service.initialize(mode=ContextMode.PRIMARY, adapter="codex")
        assert status.ready is False
        assert any(item.startswith("context_vector_") for item in status.diagnostics)
    finally:
        service.close()
