"""GGUF 本地 Embedding 引擎——通过 llama-cpp-python 加载量化模型。"""

from pathlib import Path
from hermes_memory.config import Config


class EmbeddingEngine:
    """本地 Embedding 引擎。

    使用 llama-cpp-python 加载 GGUF 格式的 embedding 模型（如 bge-small-zh）。
    启动时加载一次，进程常驻内存。
    """

    def __init__(self, config: Config):
        self.config = config
        self._model = None
        self._dim: int | None = None

    # ---- 生命周期 ----

    def initialize(self) -> None:
        """加载 GGUF embedding 模型。"""
        model_path = self.config.model_path
        if not model_path.exists():
            raise FileNotFoundError(
                f"模型文件不存在: {model_path}\n"
                f"请将 GGUF embedding 模型放置到 "
                f"{self.config.data_dir / 'models'}/ 目录"
            )

        # 延迟导入，避免 llama-cpp-python 未安装时整个模块崩溃
        from llama_cpp import Llama

        self._model = Llama(
            model_path=str(model_path),
            embedding=True,
            n_ctx=512,          # embedding 不需要长上下文
            n_batch=32,         # 批量处理
            verbose=False,
        )
        self._dim = self.config.embedding_dim

    def close(self) -> None:
        if self._model is not None:
            self._model.close()
            self._model = None

    def __enter__(self):
        self.initialize()
        return self

    def __exit__(self, *args):
        self.close()

    @property
    def is_loaded(self) -> bool:
        return self._model is not None

    @property
    def dim(self) -> int:
        if self._dim is None:
            raise RuntimeError("EmbeddingEngine 未初始化")
        return self._dim

    # ---- 编码 ----

    def encode(self, text: str) -> list[float]:
        """将单条文本编码为 embedding 向量。"""
        self._ensure_loaded()
        result = self._model.embed(text)
        # llama-cpp-python 返回 embedding 列表的列表，取第一个
        if isinstance(result, list) and len(result) > 0:
            if isinstance(result[0], list):
                return result[0]
            return result
        return result

    def encode_batch(self, texts: list[str]) -> list[list[float]]:
        """批量编码多条文本。"""
        self._ensure_loaded()
        embeddings = []
        for text in texts:
            embeddings.append(self.encode(text))
        return embeddings

    def _ensure_loaded(self):
        if self._model is None:
            raise RuntimeError(
                "EmbeddingEngine 未初始化，请先调用 initialize()"
            )
