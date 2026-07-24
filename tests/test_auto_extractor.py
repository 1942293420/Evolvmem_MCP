"""AutoExtractor tests."""

import pytest
from evolvmem.auto_extractor import AutoExtractor, CandidateMemory


class TestAutoExtractor:
    def test_format_extraction_prompt_includes_rules(self):
        extractor = AutoExtractor()
        prompt = extractor.build_extraction_prompt(
            messages=[{"role": "user", "content": "Let's use PostgreSQL"}],
        )
        assert "PostgreSQL" in prompt
        assert "Retention" in prompt or "extract" in prompt
        assert "Stable Key" in prompt or "key" in prompt

    def test_parse_empty_response(self):
        extractor = AutoExtractor()
        candidates = extractor.parse_response("Nothing worth persisting in this conversation.")
        assert candidates == []

    def test_parse_single_candidate(self):
        extractor = AutoExtractor()
        response = """```json
[{"key": "project:db:decision:engine", "value": "Use PostgreSQL as primary database", "category": "decision", "tags": ["database", "PostgreSQL"], "confidence": 0.95}]
```"""
        candidates = extractor.parse_response(response)
        assert len(candidates) == 1
        assert candidates[0].key == "project:db:decision:engine"
        assert candidates[0].value == "Use PostgreSQL as primary database"

    def test_should_persist_decision(self):
        extractor = AutoExtractor()
        c = CandidateMemory(
            key="p:x:decision:y",
            value="Chose plan A",
            category="decision",
            tags=["architecture"],
            confidence=0.9,
        )
        assert extractor.should_persist(c) is True

    def test_should_not_persist_greeting(self):
        extractor = AutoExtractor()
        c = CandidateMemory(
            key="chat:greeting",
            value="User said hello",
            category="chat",
            tags=["small_talk"],
            confidence=0.1,
        )
        assert extractor.should_persist(c) is False

    def test_build_key_sanitizes_input(self):
        extractor = AutoExtractor()
        key = extractor.build_key(
            project="my-shop",
            domain="aftersales",
            category="decision",
            topic="refund_rules",
        )
        assert key.startswith("my-shop:")
        assert " " not in key
