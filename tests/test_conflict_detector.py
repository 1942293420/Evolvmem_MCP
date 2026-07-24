"""ConflictDetector 测试。"""

import pytest
from hermes_memory.memory_store import MemoryStore
from hermes_memory.conflict_detector import ConflictDetector, ConflictDecision


class TestConflictDetector:
    def test_new_key_returns_add(self, test_config):
        store = MemoryStore(test_config)
        store.initialize()
        detector = ConflictDetector(store)

        decision = detector.check(
            candidate_key="project:new:fact:x",
            candidate_value="新信息",
        )
        assert decision.action == "add"
        store.close()

    def test_same_value_returns_skip(self, test_config):
        store = MemoryStore(test_config)
        store.initialize()
        store.add(key="project:existing:fact", value="相同内容")
        detector = ConflictDetector(store)

        decision = detector.check(
            candidate_key="project:existing:fact",
            candidate_value="相同内容",
        )
        assert decision.action == "skip"
        store.close()

    def test_user_override_returns_replace(self, test_config):
        store = MemoryStore(test_config)
        store.initialize()
        store.add(key="project:existing:decision", value="旧方案")
        detector = ConflictDetector(store)

        decision = detector.check(
            candidate_key="project:existing:decision",
            candidate_value="新方案，放弃旧方案",
            user_override=True,  # 用户明确说放弃旧方案
        )
        assert decision.action == "replace"
        store.close()

    def test_different_value_no_context_returns_conflict(self, test_config):
        store = MemoryStore(test_config)
        store.initialize()
        store.add(key="project:existing:rule", value="规则A")
        detector = ConflictDetector(store)

        decision = detector.check(
            candidate_key="project:existing:rule",
            candidate_value="规则B",
        )
        assert decision.action == "conflict"
        store.close()

    def test_newer_session_more_specific_wins(self, test_config):
        store = MemoryStore(test_config)
        store.initialize()
        store.add(key="project:existing:rule", value="默认策略")
        detector = ConflictDetector(store)

        # 来自当前 session 的更具体信息应 wins
        decision = detector.check(
            candidate_key="project:existing:rule",
            candidate_value="默认策略已改为针对VIP客户的特殊策略",
        )
        # 更具体（更长） + 来自当前 session → replace
        assert decision.action == "replace"
        store.close()
