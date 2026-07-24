"""AutoExtractor 测试。"""

import pytest
from hermes_memory.auto_extractor import AutoExtractor, CandidateMemory


class TestAutoExtractor:
    def test_format_extraction_prompt_includes_rules(self):
        extractor = AutoExtractor()
        prompt = extractor.build_extraction_prompt(
            messages=[{"role": "user", "content": "我们用 PostgreSQL 吧"}],
        )
        assert "PostgreSQL" in prompt
        assert "保留" in prompt or "提取" in prompt
        assert "稳定 key" in prompt or "key" in prompt

    def test_parse_empty_response(self):
        extractor = AutoExtractor()
        candidates = extractor.parse_response("本次对话无需要持久化的信息。")
        assert candidates == []

    def test_parse_single_candidate(self):
        extractor = AutoExtractor()
        response = """```json
[{"key": "project:db:decision:engine", "value": "使用 PostgreSQL 作为主数据库", "category": "decision", "tags": ["数据库", "PostgreSQL"], "confidence": 0.95}]
```"""
        candidates = extractor.parse_response(response)
        assert len(candidates) == 1
        assert candidates[0].key == "project:db:decision:engine"
        assert candidates[0].value == "使用 PostgreSQL 作为主数据库"

    def test_should_persist_decision(self):
        extractor = AutoExtractor()
        c = CandidateMemory(
            key="p:x:decision:y",
            value="选择了方案A",
            category="decision",
            tags=["架构"],
            confidence=0.9,
        )
        assert extractor.should_persist(c) is True

    def test_should_not_persist_greeting(self):
        extractor = AutoExtractor()
        c = CandidateMemory(
            key="chat:greeting",
            value="用户说你好",
            category="chat",
            tags=["闲聊"],
            confidence=0.1,
        )
        assert extractor.should_persist(c) is False

    def test_build_key_sanitizes_input(self):
        extractor = AutoExtractor()
        key = extractor.build_key(
            project="my-shop",
            domain="售后",
            category="decision",
            topic="退款规则",
        )
        assert key.startswith("my-shop:")
        assert " " not in key
