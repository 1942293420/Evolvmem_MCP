"""In-process local GGUF bge-base-zh-v1.5 (768-dim) embedder with token-budget batching."""
from __future__ import annotations

import math
from pathlib import Path
import threading
from typing import Any

DEFAULT_BGE_MODEL_PATH = Path("/home/jiangli/.claude/evolvmem/models/bge-base-zh-v1.5.f16.gguf")
EMBEDDING_DIM = 768
QUERY_PREFIX = "为这个句子生成表示以用于检索相关文章："
_MAX_DOC_TOKENS = 440


def l2_normalize(raw: list[float]) -> list[float]:
    if len(raw) != EMBEDDING_DIM:
        raise ValueError(f"unexpected_embedding_dim:{len(raw)}")
    norm = math.sqrt(sum(float(x) * float(x) for x in raw))
    if not math.isfinite(norm) or norm <= 0.0:
        raise ValueError("zero_or_nonfinite_embedding_norm")
    return [float(x) / norm for x in raw]


class LocalBgeEmbedder:
    """Thread-safe local llama-cpp embedder producing 768-dim unit vectors (0 LLM calls)."""

    def __init__(self, model_path: Path | str = DEFAULT_BGE_MODEL_PATH, *, n_threads: int = 6) -> None:
        self.model_path = Path(model_path).expanduser()
        self.n_threads = max(1, int(n_threads))
        self._lock = threading.Lock()
        self._model: Any = None
        self.llm_calls = 0

    def __enter__(self) -> LocalBgeEmbedder:
        self.open()
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def open(self) -> None:
        with self._lock:
            if self._model is not None:
                return
            if not self.model_path.is_file():
                raise FileNotFoundError(f"missing_local_bge_model:{self.model_path}")
            from llama_cpp import Llama
            self._model = Llama(
                model_path=str(self.model_path),
                embedding=True,
                n_ctx=512,
                n_batch=512,
                n_ubatch=512,
                n_threads=self.n_threads,
                verbose=False,
            )

    def close(self) -> None:
        with self._lock:
            m = self._model
            self._model = None
            if m is not None:
                m.close()

    def _safe_truncate(self, text: str) -> tuple[str, int]:
        cleaned = (text or "").strip()
        if not cleaned:
            return "", 0
        toks = self._model.tokenize(cleaned.encode("utf-8", errors="ignore"))
        if len(toks) > _MAX_DOC_TOKENS:
            toks = toks[:_MAX_DOC_TOKENS]
            cleaned = self._model.detokenize(toks).decode("utf-8", errors="ignore").strip()
            toks = self._model.tokenize(cleaned.encode("utf-8", errors="ignore"))
        return cleaned, max(1, len(toks))

    def encode_query(self, query: str) -> list[float] | None:
        q = (query or "").strip()
        if not q:
            return None
        return self.encode_documents([QUERY_PREFIX + q])[0]

    def encode_document(self, text: str) -> list[float] | None:
        t = (text or "").strip()
        if not t:
            return None
        return self.encode_documents([t])[0]

    def encode_documents(self, texts: list[str]) -> list[list[float] | None]:
        self.open()
        results: list[list[float] | None] = [None] * len(texts)
        with self._lock:
            packed: list[tuple[int, str, int]] = []
            for idx, raw_text in enumerate(texts):
                safe, tok_len = self._safe_truncate(raw_text)
                if safe:
                    packed.append((idx, safe, tok_len))

            pos = 0
            while pos < len(packed):
                batch_items: list[tuple[int, str]] = []
                tok_sum = 0
                while pos < len(packed):
                    idx, safe, tok_len = packed[pos]
                    if batch_items and tok_sum + tok_len + 4 > _MAX_DOC_TOKENS:
                        break
                    batch_items.append((idx, safe))
                    tok_sum += tok_len + 2
                    pos += 1
                raw_vecs = self._model.embed([txt for _, txt in batch_items])
                if raw_vecs and not isinstance(raw_vecs[0], list):
                    raw_vecs = [raw_vecs]
                for (orig_idx, _), rvec in zip(batch_items, raw_vecs):
                    results[orig_idx] = l2_normalize(rvec)
        return results
