"""USearch HNSW 向量索引——只存 (id, embedding)，作为 SQLite 的读缓存。"""

import numpy as np
from pathlib import Path
from usearch.index import Index, MetricKind, ScalarKind

from hermes_memory.config import Config


class VectorIndex:
    """USearch HNSW 向量索引包装。

    只存储 (id, embedding) 对。id 对应 MemoryStore 中 memories 表的 rowid。
    索引文件通过 mmap 加载，启动零拷贝。
    """

    def __init__(self, config: Config):
        self.config = config
        self._index: Index | None = None
        self._dim: int | None = None

    # ---- 生命周期 ----

    def initialize(self, dim: int = 512) -> None:
        """创建或加载索引。如果已有文件则 mmap 加载，否则新建。"""
        self._dim = dim
        path = str(self.config.vector_path)
        if Path(path).exists():
            self._index = Index.restore(path, view=True)
        else:
            self._index = Index(
                ndim=dim,
                metric=MetricKind.Cos,
                dtype=ScalarKind.F32,
            )

    def close(self) -> None:
        if self._index is not None:
            self._index = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    # ---- 写入 ----

    def add(self, mem_id: int, embedding: np.ndarray) -> None:
        """添加单个向量。"""
        self._ensure_initialized()
        vec = embedding.astype(np.float32)
        if vec.ndim != 1 or len(vec) != self._dim:
            raise ValueError(
                f"期望 {self._dim} 维向量，收到 shape={vec.shape}"
            )
        self._index.add(mem_id, vec)

    def add_batch(self, ids: list[int],
                  embeddings: list[np.ndarray]) -> None:
        """批量添加向量。"""
        for mid, emb in zip(ids, embeddings):
            self.add(mid, emb)

    def rebuild(self, ids: list[int],
                embeddings: list[np.ndarray]) -> None:
        """全量重建索引（SQLite 为源，崩溃恢复时调用）。"""
        assert self._dim is not None
        # 创建全新内存索引（不传入 path，避免加载旧数据导致重复 key 报错）
        self._index = Index(
            ndim=self._dim,
            metric=MetricKind.Cos,
            dtype=ScalarKind.F32,
        )
        self.add_batch(ids, embeddings)
        self.save()

    def save(self) -> None:
        """持久化到磁盘。"""
        self._ensure_initialized()
        path = str(self.config.vector_path)
        self._index.save(path)

    # ---- 查询 ----

    def search(self, embedding: np.ndarray, k: int = 20) -> list[dict]:
        """HNSW 近似最近邻搜索。返回 [{id, distance}, ...]，按距离升序。"""
        self._ensure_initialized()
        if self.count() == 0:
            return []
        vec = embedding.astype(np.float32)
        if vec.ndim != 1 or len(vec) != self._dim:
            raise ValueError(
                f"期望 {self._dim} 维向量，收到 shape={vec.shape}"
            )
        results = self._index.search(vec, min(k, self.count()))
        return [
            {"id": int(match.key), "distance": float(match.distance)}
            for match in results
        ]

    # ---- 状态 ----

    def count(self) -> int:
        self._ensure_initialized()
        return len(self._index)

    def check_consistency(self, expected_count: int) -> bool:
        """检查 USearch 索引条目数是否与 SQLite 一致。"""
        return self.count() == expected_count

    def _ensure_initialized(self):
        if self._index is None:
            raise RuntimeError("VectorIndex 未初始化，请先调用 initialize()")
