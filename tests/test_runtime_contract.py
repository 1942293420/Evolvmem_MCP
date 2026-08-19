"""Runtime-contract tests for the local embedding stack."""

import json
import os
import stat
import sys
import types
from pathlib import Path

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


@pytest.mark.parametrize(
    "field_name",
    [
        "embedding_dim",
        "context_l0_max_chars",
        "context_l1_max_chars",
        "context_l2_max_chars",
        "context_archive_ttl_days",
    ],
)
def test_boolean_numeric_runtime_settings_are_rejected(temp_dir, field_name):
    """JSON booleans must not be accepted as embedding dimensions or limits."""
    config = Config(data_dir=temp_dir)
    setattr(config, field_name, True)

    diagnostics = config.validate_runtime()

    assert any("positive integer" in message for message in diagnostics)


def test_embedding_backend_error_is_not_logged_verbatim(temp_dir, monkeypatch):
    """Backend failures must not leak paths or credentials through startup logs."""
    from evolvmem import mcp_server

    class Index:
        def initialize(self, *, dim):
            assert dim == 768

        def check_consistency(self, expected_count):
            assert expected_count == 0
            return True

    class FailingEngine:
        def initialize(self):
            raise RuntimeError("backend failed at /home/alice/secret.gguf token=secret")

    server = mcp_server.MemoryMCPServer(config=Config(data_dir=temp_dir))
    server.vidx = Index()
    server.engine = FailingEngine()
    logs = []
    server._log = logs.append
    monkeypatch.setattr(mcp_server, "Retriever", lambda *_args: object())
    monkeypatch.setattr(mcp_server, "ConflictDetector", lambda *_args: object())
    monkeypatch.setattr(mcp_server, "ForgettingEngine", lambda *_args: object())
    monkeypatch.setattr(mcp_server, "Consolidator", lambda *_args: object())

    server.initialize()

    assert logs == ["Embedding engine unavailable; FTS-only mode"]


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


def test_context_configuration_defaults_match_the_frozen_design_values(
        temp_dir, monkeypatch):
    """Every independent Context default is frozen by the design; drift breaks gates."""
    monkeypatch.delenv("EVOLVMEM_CONTEXT_MODE", raising=False)
    monkeypatch.delenv("EVOLVMEM_ADAPTER", raising=False)
    config = Config(data_dir=temp_dir)

    assert config.context_mode == "legacy"
    assert config.adapter == ""
    assert config.context_inject_max_chars == 6000
    assert config.context_inject_max_items == 12
    assert config.context_inject_pinned_max_chars == 1500
    assert config.context_inject_project_max_chars == 3000
    assert config.context_inject_related_max_chars == 1500
    assert config.context_min_confidence == 0.55
    assert config.context_vector_min_similarity == 0.80
    assert config.context_fts_weight == 0.60
    assert config.context_vector_weight == 0.40
    assert config.context_score_relevance_weight == 0.35
    assert config.context_score_project_weight == 0.15
    assert config.context_score_type_weight == 0.10
    assert config.context_score_confidence_weight == 0.10
    assert config.context_score_importance_weight == 0.10
    assert config.context_score_evidence_weight == 0.05
    assert config.context_score_recency_weight == 0.10
    assert config.context_score_frequency_weight == 0.05
    assert config.context_recency_tau_days == 30.0
    assert config.context_frequency_cap == 20
    assert config.context_project_aliases == {}
    assert config.context_archive_ttl_days == 30
    assert config.context_promotion_min_successes == 2
    assert config.context_playbook_min_experiences == 3
    assert config.context_promotion_similarity_threshold == 0.95
    assert config.validate_runtime() == ()


def test_context_mode_and_adapter_come_from_json_when_env_is_absent(
        temp_dir, monkeypatch):
    """The persisted mode survives load so a formal cutover can stick."""
    monkeypatch.delenv("EVOLVMEM_CONTEXT_MODE", raising=False)
    monkeypatch.delenv("EVOLVMEM_ADAPTER", raising=False)
    config_path = temp_dir / "config.json"
    config_path.write_text(json.dumps({"context_mode": "compat", "adapter": "dsh"}))

    loaded = Config.from_file(config_path)

    assert loaded.context_mode == "compat"
    assert loaded.adapter == "dsh"


def test_context_environment_overrides_json_config(temp_dir, monkeypatch):
    """The Codex MCP stanza must be able to override one process to primary."""
    config_path = temp_dir / "config.json"
    config_path.write_text(json.dumps({"context_mode": "compat", "adapter": "claude"}))
    monkeypatch.setenv("EVOLVMEM_CONTEXT_MODE", "primary")
    monkeypatch.setenv("EVOLVMEM_ADAPTER", "codex")

    loaded = Config.from_file(config_path)

    assert loaded.context_mode == "primary"
    assert loaded.adapter == "codex"


def test_unknown_context_mode_is_a_structured_diagnostic_and_never_primary(
        temp_dir, monkeypatch):
    """An unknown mode fails closed: it is reported, never coerced to primary."""
    monkeypatch.setenv("EVOLVMEM_CONTEXT_MODE", "turbo")
    config = Config(data_dir=temp_dir)

    diagnostics = config.validate_runtime()

    assert config.context_mode != "primary"
    assert any("context_mode" in message for message in diagnostics)


@pytest.mark.parametrize(
    "field_name",
    [
        "context_inject_max_chars",
        "context_inject_max_items",
        "context_inject_pinned_max_chars",
        "context_inject_project_max_chars",
        "context_inject_related_max_chars",
        "context_frequency_cap",
        "context_archive_ttl_days",
        "context_promotion_min_successes",
        "context_playbook_min_experiences",
    ],
)
@pytest.mark.parametrize("bad", [0, -1, True])
def test_context_positive_integer_settings_are_validated(temp_dir, field_name, bad):
    """Budget and cap values of zero, negative, or boolean must be diagnosed."""
    config = Config(data_dir=temp_dir)
    setattr(config, field_name, bad)

    diagnostics = config.validate_runtime()

    assert any(field_name in message for message in diagnostics)


@pytest.mark.parametrize(
    "field_name",
    [
        "context_min_confidence",
        "context_vector_min_similarity",
        "context_promotion_similarity_threshold",
    ],
)
@pytest.mark.parametrize("bad", [-0.1, 1.1, True, float("nan"), float("inf")])
def test_context_unit_interval_settings_are_validated(temp_dir, field_name, bad):
    """Confidence/similarity thresholds outside 0..1 would corrupt gating."""
    config = Config(data_dir=temp_dir)
    setattr(config, field_name, bad)

    diagnostics = config.validate_runtime()

    assert any(field_name in message for message in diagnostics)


@pytest.mark.parametrize("bad", [0.0, -2.0, float("inf"), True])
def test_context_recency_tau_must_be_positive_and_finite(temp_dir, bad):
    """A non-positive or infinite tau would make the recency component meaningless."""
    config = Config(data_dir=temp_dir)
    config.context_recency_tau_days = bad

    diagnostics = config.validate_runtime()

    assert any("context_recency_tau_days" in message for message in diagnostics)


def test_context_retrieval_weights_must_sum_to_one(temp_dir):
    """The lexical/vector fusion weights are only meaningful when they total 1.0."""
    config = Config(data_dir=temp_dir)
    config.context_fts_weight = 0.7

    diagnostics = config.validate_runtime()

    assert any(
        "context_fts_weight" in message and "context_vector_weight" in message
        for message in diagnostics
    )


def test_context_score_weights_must_sum_to_one(temp_dir):
    """The eight independent score weights must total 1.0 on their own."""
    config = Config(data_dir=temp_dir)
    config.context_score_relevance_weight = 0.45

    diagnostics = config.validate_runtime()

    assert any("context_score" in message for message in diagnostics)


@pytest.mark.parametrize(
    "field_name, value",
    [
        ("context_fts_weight", True),
        ("context_fts_weight", 1.2),
        ("context_score_recency_weight", True),
        ("context_score_recency_weight", 1.5),
    ],
)
def test_context_weights_reject_booleans_and_out_of_range_values(
        temp_dir, field_name, value):
    """A boolean weight pair can still sum to 1.0; type checks must catch it first."""
    config = Config(data_dir=temp_dir)
    if field_name == "context_fts_weight" and value is True:
        config.context_vector_weight = False
    setattr(config, field_name, value)

    diagnostics = config.validate_runtime()

    assert any(field_name.split("_weight")[0] in message for message in diagnostics)


def test_config_round_trip_preserves_context_configuration(temp_dir, monkeypatch):
    """Dropping any persisted Context field would silently reset cutover state."""
    monkeypatch.delenv("EVOLVMEM_CONTEXT_MODE", raising=False)
    monkeypatch.delenv("EVOLVMEM_ADAPTER", raising=False)
    config = Config(data_dir=temp_dir)
    config.context_mode = "compat"
    config.adapter = "kimi"
    config.context_inject_max_chars = 5000
    config.context_inject_max_items = 9
    config.context_inject_pinned_max_chars = 1200
    config.context_inject_project_max_chars = 2500
    config.context_inject_related_max_chars = 1100
    config.context_min_confidence = 0.6
    config.context_vector_min_similarity = 0.75
    config.context_fts_weight = 0.55
    config.context_vector_weight = 0.45
    config.context_score_relevance_weight = 0.30
    config.context_score_project_weight = 0.20
    config.context_recency_tau_days = 14.0
    config.context_frequency_cap = 10
    config.context_project_aliases = {"hermes-memory-plugin": "evolvmem"}
    config.context_archive_ttl_days = 14
    config.context_promotion_min_successes = 3
    config.context_playbook_min_experiences = 4
    config.context_promotion_similarity_threshold = 0.97
    config.save()

    loaded = Config.from_file(config.config_path)

    assert loaded.context_mode == "compat"
    assert loaded.adapter == "kimi"
    assert loaded.context_inject_max_chars == 5000
    assert loaded.context_inject_max_items == 9
    assert loaded.context_inject_pinned_max_chars == 1200
    assert loaded.context_inject_project_max_chars == 2500
    assert loaded.context_inject_related_max_chars == 1100
    assert loaded.context_min_confidence == 0.6
    assert loaded.context_vector_min_similarity == 0.75
    assert loaded.context_fts_weight == 0.55
    assert loaded.context_vector_weight == 0.45
    assert loaded.context_score_relevance_weight == 0.30
    assert loaded.context_score_project_weight == 0.20
    assert loaded.context_recency_tau_days == 14.0
    assert loaded.context_frequency_cap == 10
    assert loaded.context_project_aliases == {"hermes-memory-plugin": "evolvmem"}
    assert loaded.context_archive_ttl_days == 14
    assert loaded.context_promotion_min_successes == 3
    assert loaded.context_playbook_min_experiences == 4
    assert loaded.context_promotion_similarity_threshold == 0.97


def test_config_save_replaces_via_a_same_directory_temp_file(temp_dir, monkeypatch):
    """Atomic replacement requires a sibling temp file, never an in-place rewrite."""
    config = Config(data_dir=temp_dir)
    config.save()
    replace_calls = []
    real_replace = os.replace

    def spy_replace(src, dst):
        replace_calls.append((Path(src), Path(dst)))
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", spy_replace)
    config.context_mode = "shadow"
    config.save()

    assert len(replace_calls) == 1
    temp_path, destination = replace_calls[0]
    assert temp_path.parent == config.config_path.parent
    assert temp_path.name != config.config_path.name
    assert destination == config.config_path
    assert json.loads(config.config_path.read_text())["context_mode"] == "shadow"
    assert not list(temp_dir.glob("*.tmp"))


def test_config_save_fsyncs_the_file_and_parent_directory(temp_dir, monkeypatch):
    """Durability needs fsync on both the payload and the directory entry."""
    config = Config(data_dir=temp_dir)
    fsynced = []
    real_fsync = os.fsync

    def spy_fsync(fd):
        fsynced.append(fd)
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", spy_fsync)
    config.save()

    assert len(fsynced) >= 2


def test_config_save_failure_leaves_the_previous_json_byte_for_byte_intact(
        temp_dir, monkeypatch):
    """A failed replace must not truncate or corrupt the last good configuration."""
    config = Config(data_dir=temp_dir)
    config.save()
    before = config.config_path.read_bytes()

    def fail_replace(src, dst):
        raise OSError("synthetic replace failure")

    monkeypatch.setattr(os, "replace", fail_replace)
    config.context_mode = "primary"
    with pytest.raises(OSError, match="synthetic replace failure"):
        config.save()

    assert config.config_path.read_bytes() == before
    assert not list(temp_dir.glob("*.tmp"))


def test_config_save_preserves_existing_file_mode_bits(temp_dir):
    """Replacing the config must not widen or tighten the owner's permissions."""
    config = Config(data_dir=temp_dir)
    config.save()
    os.chmod(config.config_path, 0o640)

    config.save()

    assert stat.S_IMODE(config.config_path.stat().st_mode) == 0o640
