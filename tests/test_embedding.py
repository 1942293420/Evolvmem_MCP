"""EmbeddingEngine 测试。"""

import pytest
from hermes_memory.embedding import EmbeddingEngine


class TestEmbeddingEngine:
    def test_initialize_fails_gracefully_without_model(self, test_config):
        """模型文件不存在时应该给出明确错误。"""
        engine = EmbeddingEngine(test_config)
        with pytest.raises(FileNotFoundError, match="模型文件不存在"):
            engine.initialize()

    def test_is_loaded_false_before_initialize(self, test_config):
        """初始化前 is_loaded 应为 False。"""
        engine = EmbeddingEngine(test_config)
        assert engine.is_loaded is False

    def test_dim_raises_before_initialize(self, test_config):
        """初始化前访问 dim 应抛出 RuntimeError。"""
        engine = EmbeddingEngine(test_config)
        with pytest.raises(RuntimeError, match="未初始化"):
            _ = engine.dim

    def test_encode_raises_before_initialize(self, test_config):
        """初始化前调用 encode 应抛出 RuntimeError。"""
        engine = EmbeddingEngine(test_config)
        with pytest.raises(RuntimeError, match="未初始化"):
            engine.encode("hello")

    def test_encode_batch_raises_before_initialize(self, test_config):
        """初始化前调用 encode_batch 应抛出 RuntimeError。"""
        engine = EmbeddingEngine(test_config)
        with pytest.raises(RuntimeError, match="未初始化"):
            engine.encode_batch(["hello", "world"])

    def test_context_manager_raises_without_model(self, test_config):
        """上下文管理器在模型不存在时也应抛出 FileNotFoundError。"""
        with pytest.raises(FileNotFoundError, match="模型文件不存在"):
            with EmbeddingEngine(test_config) as engine:
                pass

    def test_close_before_initialize_is_safe(self, test_config):
        """未初始化时调用 close 应安全（无操作）。"""
        engine = EmbeddingEngine(test_config)
        engine.close()  # 不应抛出异常
        assert engine.is_loaded is False

    # ---- 以下测试需要真实 GGUF 模型 + llama-cpp-python ----

    @pytest.mark.skip(reason="需要真实 GGUF embedding 模型")
    def test_embedding_shape_matches_config(self, test_config):
        """需要真实 GGUF 模型才能运行，此处验证接口签名。"""
        engine = EmbeddingEngine(test_config)
        engine.initialize()
        try:
            vec = engine.encode("测试文本")
            assert len(vec) == test_config.embedding_dim
            assert all(isinstance(v, float) for v in vec)
        finally:
            engine.close()

    @pytest.mark.skip(reason="需要真实 GGUF embedding 模型")
    def test_batch_encode_same_as_single(self, test_config):
        """批量编码结果应与单条编码一致。"""
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
