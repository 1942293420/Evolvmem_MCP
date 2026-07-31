"""VectorIndex 测试。"""

import numpy as np
import pytest
from evolvmem.vector_index import VectorIndex


def make_embedding(dim=512):
    """生成随机归一化向量。"""
    v = np.random.randn(dim).astype(np.float32)
    return v / np.linalg.norm(v)


class TestVectorIndex:
    def test_initialize_creates_index(self, test_config):
        idx = VectorIndex(test_config)
        idx.initialize(dim=512)
        assert idx.count() == 0
        idx.close()

    def test_add_and_search(self, test_config):
        idx = VectorIndex(test_config)
        idx.initialize(dim=512)

        # 添加 3 条向量
        v1 = np.ones(512, dtype=np.float32) / np.sqrt(512)
        v2 = np.zeros(512, dtype=np.float32)
        v2[0] = 1.0
        v3 = -np.ones(512, dtype=np.float32) / np.sqrt(512)

        idx.add(1, v1)
        idx.add(2, v2)
        idx.add(3, v3)
        idx.save()

        # 搜索
        results = idx.search(v1, k=3)
        ids = [r["id"] for r in results]
        assert ids[0] == 1  # v1 最接近自己
        idx.close()

    def test_remove_keeps_count_in_sync(self, test_config):
        idx = VectorIndex(test_config)
        idx.initialize(dim=512)
        v = np.ones(512, dtype=np.float32) / np.sqrt(512)

        idx.add(1, v)
        idx.add(2, v)
        assert idx.count() == 2

        assert idx.remove(1) is True
        assert idx.remove(999) is False  # 不存在的 id 不抛异常
        idx.save()

        assert idx.count() == 1
        results = idx.search(v, k=2)
        assert [r["id"] for r in results] == [2]
        idx.close()

    def test_persistence_survives_reopen(self, test_config):
        idx = VectorIndex(test_config)
        idx.initialize(dim=512)
        v = np.ones(512, dtype=np.float32) / np.sqrt(512)
        idx.add(42, v)
        idx.save()
        idx.close()

        # 重新打开
        idx2 = VectorIndex(test_config)
        idx2.initialize(dim=512)
        assert idx2.count() == 1
        results = idx2.search(v, k=1)
        assert results[0]["id"] == 42
        idx2.close()

    def test_rebuild_from_ids(self, test_config):
        """模拟崩溃后从 SQLite 重建。"""
        idx = VectorIndex(test_config)
        idx.initialize(dim=512)

        ids = [1, 2, 3]
        embeddings = [make_embedding() for _ in range(3)]
        idx.add_batch(ids, embeddings)
        idx.save()

        # 模拟重建：用不同 ID 和不同向量替换全部数据
        new_ids = [4, 5]
        new_embeddings = [make_embedding() for _ in range(2)]
        idx2 = VectorIndex(test_config)
        idx2.initialize(dim=512)
        idx2.rebuild(new_ids, new_embeddings)  # 全量替换
        assert idx2.count() == 2

        # 确认旧 ID 不再存在，新 ID 可检索
        results = idx2.search(new_embeddings[0], k=2)
        found_ids = [r["id"] for r in results]
        assert 4 in found_ids and 5 in found_ids
        assert 1 not in found_ids and 2 not in found_ids and 3 not in found_ids
        idx2.close()

    def test_sync_consistency(self, test_config):
        """检测 USearch 与 SQLite 记录数是否一致。"""
        idx = VectorIndex(test_config)
        idx.initialize(dim=512)
        # 初始状态：一致
        assert idx.check_consistency(expected_count=0)

        idx.add(1, make_embedding())
        idx.save()
        assert idx.check_consistency(expected_count=1)
        assert not idx.check_consistency(expected_count=2)  # 不一致
        idx.close()

    def test_empty_search_returns_empty(self, test_config):
        idx = VectorIndex(test_config)
        idx.initialize(dim=512)
        results = idx.search(make_embedding(), k=10)
        assert results == []
        idx.close()
