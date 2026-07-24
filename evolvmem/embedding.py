"""GGUF local embedding engine — loads quantized models via llama-cpp-python."""

from pathlib import Path
from evolvmem.config import Config


class EmbeddingEngine:
    """Local embedding engine.

    Uses llama-cpp-python to load GGUF-format embedding models (e.g., bge-small-zh).
    Loads once at startup, stays resident in process memory.
    """

    def __init__(self, config: Config):
        self.config = config
        self._model = None
        self._dim: int | None = None

    # ---- lifecycle ----

    def initialize(self) -> None:
        """Load the GGUF embedding model."""
        model_path = self.config.model_path
        if not model_path.exists():
            raise FileNotFoundError(
                f"Model file not found: {model_path}\n"
                f"Place the GGUF embedding model in "
                f"{self.config.data_dir / 'models'}/ directory"
            )

        # Lazy import to avoid crashing the whole module if llama-cpp-python is not installed
        from llama_cpp import Llama

        self._model = Llama(
            model_path=str(model_path),
            embedding=True,
            n_ctx=512,          # embedding doesn't need long context
            n_batch=32,         # batch processing
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
            raise RuntimeError("EmbeddingEngine not initialized")
        return self._dim

    # ---- encoding ----

    def encode(self, text: str) -> list[float]:
        """Encode a single text to an embedding vector."""
        self._ensure_loaded()
        result = self._model.embed(text)
        # llama-cpp-python returns embeddings as list of lists, take the first
        if isinstance(result, list) and len(result) > 0:
            if isinstance(result[0], list):
                return result[0]
            return result
        return result

    def encode_batch(self, texts: list[str]) -> list[list[float]]:
        """Batch encode multiple texts."""
        self._ensure_loaded()
        embeddings = []
        for text in texts:
            embeddings.append(self.encode(text))
        return embeddings

    def _ensure_loaded(self):
        if self._model is None:
            raise RuntimeError(
                "EmbeddingEngine not initialized, call initialize() first"
            )
