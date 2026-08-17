"""Runtime-contract tests for the local embedding stack."""

import sys
import types

import pytest

from evolvmem.config import Config
from evolvmem.embedding import EmbeddingEngine
from evolvmem.runtime_contract import DEFAULT_EMBEDDING_CONTRACT


def test_default_contract_describes_nomic_embedding_runtime():
    """A changed default model profile must not silently change persisted vectors."""
    contract = DEFAULT_EMBEDDING_CONTRACT

    assert contract.filename == "nomic-embed-text-v1.5.f16.gguf"
    assert contract.dimension == 768
    assert contract.query_prefix
    assert contract.document_prefix


def test_config_model_path_uses_contract_filename(temp_dir):
    """Changing the configured filename must change where the model is loaded."""
    config = Config(data_dir=temp_dir)

    assert config.model_path == temp_dir / "models" / "nomic-embed-text-v1.5.f16.gguf"


def test_config_round_trip_preserves_embedding_profile(temp_dir):
    """Dropping persisted embedding settings would change an existing vector space."""
    config = Config(data_dir=temp_dir)
    config.embedding_model_filename = "custom.gguf"
    config.embedding_dim = 384
    config.embedding_query_prefix = "query: "
    config.embedding_doc_prefix = "document: "
    config.save()

    loaded = Config.from_file(config.config_path)

    assert loaded.embedding_model_filename == "custom.gguf"
    assert loaded.embedding_dim == 384
    assert loaded.embedding_query_prefix == "query: "
    assert loaded.embedding_doc_prefix == "document: "


def test_nomic_dimension_mismatch_is_a_runtime_diagnostic(temp_dir):
    """A Nomic 512-dimensional config must not be treated as vector-compatible."""
    config = Config(data_dir=temp_dir)
    config.embedding_dim = 512

    diagnostics = config.validate_runtime()

    assert diagnostics
    assert any("embedding_dim" in message for message in diagnostics)
    assert any(config.embedding_model_filename in message for message in diagnostics)


def test_missing_model_diagnostic_redacts_home_path_and_credentials(temp_dir, monkeypatch):
    """A missing model report must remain safe to expose from memory_status."""
    monkeypatch.setenv("HOME", "/private/people/alice")
    config = Config(data_dir=temp_dir)
    config.embedding_model_filename = "missing.gguf"

    diagnostics = config.validate_runtime(require_model=True)
    rendered = "\n".join(diagnostics)

    assert "missing.gguf" in rendered
    assert "/private/people/alice" not in rendered
    assert "api_key" not in rendered.casefold()


@pytest.mark.parametrize("filename", [True, ["model.gguf"]])
def test_invalid_model_filename_returns_a_diagnostic_instead_of_raising(
        temp_dir, filename):
    """Malformed JSON must not crash fail-open embedding startup or status."""
    config = Config(data_dir=temp_dir)
    config.embedding_model_filename = filename

    diagnostics = config.validate_runtime(require_model=True)

    assert diagnostics
    assert any("embedding_model_filename" in message for message in diagnostics)


def test_path_like_model_filename_is_rejected_without_leaking_home_path(temp_dir):
    """A config filename must not escape the model directory in diagnostics."""
    config = Config(data_dir=temp_dir)
    config.embedding_model_filename = "/private/people/alice/private.gguf"

    diagnostics = config.validate_runtime(require_model=True)
    rendered = "\n".join(diagnostics)

    assert diagnostics
    assert "/private/people/alice" not in rendered
    assert any("embedding_model_filename" in message for message in diagnostics)


def test_initialize_rejects_probe_dimension_mismatch_and_clears_model(
        temp_dir, monkeypatch):
    """A wrong model output dimension must never leave vector encoding enabled."""
    config = Config(data_dir=temp_dir)
    config.model_path.parent.mkdir(parents=True)
    config.model_path.touch()

    class FakeLlama:
        closed = False

        def __init__(self, **_kwargs):
            pass

        def embed(self, _text):
            return [[0.0] * 3]

        def close(self):
            self.closed = True

    monkeypatch.setitem(sys.modules, "llama_cpp", types.SimpleNamespace(Llama=FakeLlama))
    engine = EmbeddingEngine(config)

    with pytest.raises(RuntimeError, match="embedding_dim"):
        engine.initialize()

    assert engine.is_loaded is False


def test_installer_imports_the_contract_instead_of_duplicating_defaults():
    """Installer changes must derive the model profile from the Python contract."""
    installer = (Config.__module__ and __import__("pathlib").Path(__file__).parents[1] / "install.sh").read_text()

    assert "evolvmem.runtime_contract" in installer
    assert "bge-small-zh" not in installer
    assert "embedding_dim\": 512" not in installer
