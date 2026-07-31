"""ConflictDetector tests."""

import pytest
from evolvmem.memory_store import MemoryStore
from evolvmem.conflict_detector import ConflictDetector, ConflictDecision


class TestConflictDetector:
    def test_new_key_returns_add(self, test_config):
        store = MemoryStore(test_config)
        store.initialize()
        detector = ConflictDetector(store)

        decision = detector.check(
            candidate_key="project:new:fact:x",
            candidate_value="new info",
        )
        assert decision.action == "add"
        store.close()

    def test_same_value_returns_skip(self, test_config):
        store = MemoryStore(test_config)
        store.initialize()
        store.add(key="project:existing:fact", value="same content")
        detector = ConflictDetector(store)

        decision = detector.check(
            candidate_key="project:existing:fact",
            candidate_value="same content",
        )
        assert decision.action == "skip"
        assert decision.existing_id is not None
        assert "unchanged" in decision.reason
        store.close()

    def test_user_override_returns_replace(self, test_config):
        store = MemoryStore(test_config)
        store.initialize()
        store.add(key="project:existing:decision", value="old approach")
        detector = ConflictDetector(store)

        decision = detector.check(
            candidate_key="project:existing:decision",
            candidate_value="new approach, abandon old",
            user_override=True,  # user explicitly says to abandon old approach
        )
        assert decision.action == "replace"
        assert decision.existing_id is not None
        assert "abandon" in decision.reason
        store.close()

    def test_different_value_no_context_returns_conflict(self, test_config):
        store = MemoryStore(test_config)
        store.initialize()
        store.add(key="project:existing:rule", value="rule A")
        detector = ConflictDetector(store)

        decision = detector.check(
            candidate_key="project:existing:rule",
            candidate_value="rule B",
        )
        assert decision.action == "conflict"
        assert decision.existing_id is not None
        assert "cannot" in decision.reason
        store.close()

    def test_newer_session_more_specific_wins(self, test_config):
        store = MemoryStore(test_config)
        store.initialize()
        store.add(key="project:existing:rule", value="default strategy")
        detector = ConflictDetector(store)

        # more specific info from current session should win
        decision = detector.check(
            candidate_key="project:existing:rule",
            candidate_value="default strategy changed to VIP-only special strategy",
        )
        # more specific (longer) + from current session → replace
        assert decision.action == "replace"
        assert decision.existing_id is not None
        assert "more specific" in decision.reason
        store.close()
