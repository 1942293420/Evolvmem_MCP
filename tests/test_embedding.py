"""EmbeddingEngine tests."""

import pytest
from evolvmem.embedding import EmbeddingEngine


class TestEmbeddingEngine:
    def test_initialize_fails_gracefully_without_model(self, test_config):
        """Model file not found should raise a clear error."""
        engine = EmbeddingEngine(test_config)
        with pytest.raises(FileNotFoundError, match="Model file not found"):
            engine.initialize()

    def test_is_loaded_false_before_initialize(self, test_config):
        """is_loaded should be False before initialization."""
        engine = EmbeddingEngine(test_config)
        assert engine.is_loaded is False

    def test_dim_raises_before_initialize(self, test_config):
        """Accessing dim before initialization should raise RuntimeError."""
        engine = EmbeddingEngine(test_config)
        with pytest.raises(RuntimeError, match="not initialized"):
            _ = engine.dim

    def test_encode_raises_before_initialize(self, test_config):
        """Calling encode before initialization should raise RuntimeError."""
        engine = EmbeddingEngine(test_config)
        with pytest.raises(RuntimeError, match="not initialized"):
            engine.encode("hello")

    def test_encode_batch_raises_before_initialize(self, test_config):
        """Calling encode_batch before initialization should raise RuntimeError."""
        engine = EmbeddingEngine(test_config)
        with pytest.raises(RuntimeError, match="not initialized"):
            engine.encode_batch(["hello", "world"])

    def test_context_manager_raises_without_model(self, test_config):
        """Context manager should also raise FileNotFoundError when model is missing."""
        with pytest.raises(FileNotFoundError, match="Model file not found"):
            with EmbeddingEngine(test_config) as engine:
                pass

    def test_close_before_initialize_is_safe(self, test_config):
        """Calling close before initialization should be safe (no-op)."""
        engine = EmbeddingEngine(test_config)
        engine.close()  # should not raise
        assert engine.is_loaded is False

    # ---- Tests below require a real GGUF model + llama-cpp-python ----

    @pytest.mark.skip(reason="Requires real GGUF embedding model")
    def test_embedding_shape_matches_config(self, test_config):
        """Requires real GGUF model; verifies interface signature."""
        engine = EmbeddingEngine(test_config)
        engine.initialize()
        try:
            vec = engine.encode("test text")
            assert len(vec) == test_config.embedding_dim
            assert all(isinstance(v, float) for v in vec)
        finally:
            engine.close()

    @pytest.mark.skip(reason="Requires real GGUF embedding model")
    def test_batch_encode_same_as_single(self, test_config):
        """Batch encode results should match single encode results."""
        engine = EmbeddingEngine(test_config)
        engine.initialize()
        try:
            texts = ["hello", "world"]
            batched = engine.encode_batch(texts)
            single = [engine.encode(t) for t in texts]
            assert len(batched) == len(single)
            for b, s in zip(batched, single):
                assert b == s
        finally:
            engine.close()
