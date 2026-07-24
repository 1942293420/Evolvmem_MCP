"""端到端集成测试——从写入到检索的完整链路。"""

import hashlib
import pytest
import numpy as np
from hermes_memory.config import Config
from hermes_memory.memory_store import MemoryStore
from hermes_memory.vector_index import VectorIndex
from hermes_memory.retriever import Retriever
from hermes_memory.conflict_detector import ConflictDetector
from hermes_memory.forgetting import ForgettingEngine
from hermes_memory.auto_extractor import AutoExtractor


class FakeEmbeddingEngine:
    """假 embedding 引擎，返回确定性向量（同文本 → 同向量）。"""

    def __init__(self, dim=512):
        self._dim = dim
        self._loaded = True

    @property
    def is_loaded(self):
        return self._loaded

    @property
    def dim(self):
        return self._dim

    def encode(self, text):
        """确定性随机向量（基于 text hash），保证相同 text 返回相同向量。"""
        h = hashlib.md5(text.encode()).digest()
        seed = int.from_bytes(h[:4], 'big')
        rng = np.random.RandomState(seed)
        v = rng.randn(self._dim).astype(np.float32)
        return (v / np.linalg.norm(v)).tolist()

    def encode_batch(self, texts):
        return [self.encode(t) for t in texts]


class TestIntegration:
    """完整的记忆生命周期测试。"""

    def test_full_lifecycle_write_search_replace(self, test_config):
        """完整生命周期：写入 → 检索 → 替换 → 验证历史。"""
        # 初始化
        store = MemoryStore(test_config)
        store.initialize()
        vidx = VectorIndex(test_config)
        vidx.initialize(dim=512)
        engine = FakeEmbeddingEngine()
        retriever = Retriever(test_config, store, vidx, engine)
        detector = ConflictDetector(store)

        # 1. 写入记忆
        mem1_id = store.add(
            key="project:shop:decision:after_sales",
            value="破损商品直接退款，不再补发",
            category="decision",
            tags=["售后", "退款"],
        )
        mem2_id = store.add(
            key="user:preference:theme",
            value="偏好暗色主题界面",
            category="preference",
            tags=["UI"],
        )
        mem3_id = store.add(
            key="project:db:fact:version",
            value="PostgreSQL 版本需 15 以上",
            category="fact",
            tags=["数据库"],
        )

        # 更新向量索引
        for mid in [mem1_id, mem2_id, mem3_id]:
            rec = store.get_by_id(mid)
            vec = np.array(engine.encode(rec["value"]), dtype=np.float32)
            vidx.add(mid, vec)
        vidx.save()

        # 2. FTS5 精确搜索（混合检索会返回多项，但正确结果应在其中）
        results = retriever.search("退款", top_k=5)
        assert len(results) >= 1
        result_keys = [r["key"] for r in results]
        assert "project:shop:decision:after_sales" in result_keys

        # 3. 语义搜索（换了表达方式）
        results = retriever.search("用户喜欢什么颜色", top_k=5)
        # 应命中 "暗色主题"
        keys = [r["key"] for r in results]
        assert "user:preference:theme" in keys

        # 4. 替换记忆
        decision = detector.check(
            "project:shop:decision:after_sales",
            "破损商品直接退款，不再补发；VIP 客户额外补偿优惠券",
        )
        assert decision.action == "replace"

        new_id = store.replace(
            key="project:shop:decision:after_sales",
            new_value="破损商品直接退款，不再补发；VIP 客户额外补偿优惠券",
        )
        new_vec = np.array(
            engine.encode(
                "破损商品直接退款，不再补发；VIP 客户额外补偿优惠券"
            ),
            dtype=np.float32,
        )
        vidx.add(new_id, new_vec)
        vidx.save()

        # 5. 验证：旧值 superseded，新值 active
        old = store.get_by_id(mem1_id)
        assert old["status"] == "superseded"
        new = store.get_by_id(new_id)
        assert new["status"] == "active"

        # get_active 只返回新的
        actives = store.get_active()
        after_sales_memories = [
            m for m in actives
            if m["key"] == "project:shop:decision:after_sales"
        ]
        assert len(after_sales_memories) == 1
        assert "VIP" in after_sales_memories[0]["value"]

        # 6. 历史查询仍能找到旧值
        history = store.get_by_key("project:shop:decision:after_sales")
        assert len(history) == 2  # 旧 (superseded) + 新 (active)

        # 清理
        store.close()
        vidx.close()

    def test_auto_extractor_realistic_conversation(self):
        """从真实对话中提取记忆。"""
        extractor = AutoExtractor()
        conversation = [
            {"role": "user", "content": "我们用 PostgreSQL 代替 MySQL 吧，性能更好"},
            {"role": "assistant", "content": "好的，我来调整配置"},
        ]
        prompt = extractor.build_extraction_prompt(conversation)
        assert "PostgreSQL" in prompt
        assert "MySQL" in prompt

        # 模拟 Claude 返回的 JSON
        fake_response = """```json
[
  {
    "key": "project:db:decision:engine",
    "value": "数据库选用 PostgreSQL，替代 MySQL",
    "category": "decision",
    "tags": ["数据库", "PostgreSQL", "架构"],
    "confidence": 0.95
  }
]
```"""
        candidates = extractor.parse_response(fake_response)
        assert len(candidates) == 1
        assert candidates[0].key == "project:db:decision:engine"
        assert extractor.should_persist(candidates[0]) is True

    def test_forgetting_does_not_archive_recently_used(self, test_config):
        """遗忘引擎不归档最近使用的记忆。"""
        store = MemoryStore(test_config)
        store.initialize()
        mem_id = store.add(key="p:f:1", value="活跃记忆")
        store.update_access(mem_id)

        engine = ForgettingEngine(test_config, store)
        engine.config.forget_days_threshold = 0
        engine.config.forget_access_count_threshold = 2

        candidates = engine.find_candidates()
        # access_count=1 但刚被访问，last_accessed 很新
        # 由于 forget_rate_limit_days 默认为 7，刚更新的记忆不会成为候选
        assert len(candidates) == 0

        store.close()

    def test_mcp_tool_schemas_match_design(self):
        """验证 MCP Server 注册了全部 5 个工具。"""
        from hermes_memory.mcp_server import MemoryMCPServer
        server = MemoryMCPServer()
        # 伪造 initialize request 后直接查询工具列表
        response = server._handle_request({
            "method": "tools/list", "id": 1, "jsonrpc": "2.0",
        })
        tools = response["result"]["tools"]
        tool_names = {t["name"] for t in tools}
        expected = {
            "memory_search", "memory_status", "memory_add",
            "memory_replace", "memory_remove",
        }
        assert tool_names == expected

    def test_usearch_rebuild_from_sqlite(self, test_config):
        """向量索引崩溃后从 SQLite 重建。"""
        store = MemoryStore(test_config)
        store.initialize()
        mem_id = store.add(
            key="p:rebuild:test", value="重建测试记忆", tags=["测试"]
        )
        store.close()

        vidx = VectorIndex(test_config)
        vidx.initialize(dim=512)
        engine = FakeEmbeddingEngine()

        # 模拟重建
        store2 = MemoryStore(test_config)
        store2.initialize()
        all_ids = store2.all_ids()
        records = store2.get_by_ids(all_ids)

        ids = []
        embeddings = []
        for r in records:
            vec = np.array(engine.encode(r["value"]), dtype=np.float32)
            ids.append(r["id"])
            embeddings.append(vec)

        vidx.rebuild(ids, embeddings)
        assert vidx.count() == 1
        assert vidx.check_consistency(len(all_ids))

        store2.close()
        vidx.close()
