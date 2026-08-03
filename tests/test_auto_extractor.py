"""AutoExtractor tests."""

import json

import pytest
from evolvmem.auto_extractor import AutoExtractor, CandidateMemory


def test_extraction_prompt_requires_chinese_durable_memories():
    """The provider contract must request Chinese, durable memories only."""
    prompt = AutoExtractor().build_extraction_prompt([
        {"role": "user", "content": "请记住这个长期约束"},
    ])

    assert "value 必须使用中文" in prompt
    assert "长期" in prompt
    assert "临时密码" in prompt
    assert "测试通过" in prompt
    assert "部署完成" in prompt
    assert '"memories"' in prompt
    assert "SESSION_SUMMARY" in prompt
    assert "SESSION_SUMMARY 不占 8 条原子记忆配额" in prompt


class TestAutoExtractor:
    def test_format_extraction_prompt_includes_rules(self):
        extractor = AutoExtractor()
        prompt = extractor.build_extraction_prompt(
            messages=[{"role": "user", "content": "Let's use PostgreSQL"}],
        )
        assert "PostgreSQL" in prompt
        assert "保留规则" in prompt
        assert "稳定 key 格式" in prompt

    def test_parse_empty_response(self):
        extractor = AutoExtractor()
        candidates = extractor.parse_response("Nothing worth persisting in this conversation.")
        assert candidates == []

    def test_parse_single_candidate(self):
        extractor = AutoExtractor()
        response = """```json
[{"key": "project:db:decision:engine", "value": "Use PostgreSQL as primary database", "attribute": "decision", "tags": ["database", "PostgreSQL"], "confidence": 0.95}]
```"""
        candidates = extractor.parse_response(response)
        assert len(candidates) == 1
        assert candidates[0].key == "project:db:decision:engine"
        assert candidates[0].value == "Use PostgreSQL as primary database"

    def test_parse_memories_object_protocol(self):
        extractor = AutoExtractor()
        response = """```json
{"memories": [{"key": "project:db:decision:engine", "value": "Use PostgreSQL as primary database", "attribute": "decision", "confidence": 0.95}]}
```"""

        candidates = extractor.parse_response(response)

        assert len(candidates) == 1
        assert candidates[0].key == "project:db:decision:engine"

    def test_should_persist_decision(self):
        extractor = AutoExtractor()
        c = CandidateMemory(
            key="p:x:decision:y",
            value="Chose plan A",
            attribute="decision",
            tags=["architecture"],
            confidence=0.9,
        )
        assert extractor.should_persist(c) is True

    def test_should_not_persist_greeting(self):
        extractor = AutoExtractor()
        c = CandidateMemory(
            key="chat:greeting",
            value="User said hello",
            attribute="chat",
            tags=["small_talk"],
            confidence=0.1,
        )
        assert extractor.should_persist(c) is False

    def test_build_key_sanitizes_input(self):
        extractor = AutoExtractor()
        key = extractor.build_key(
            project="my-shop",
            domain="aftersales",
            attribute="decision",
            topic="refund_rules",
        )
        assert key.startswith("my-shop:")
        assert " " not in key

    def test_parse_importance_and_tier(self):
        extractor = AutoExtractor()
        response = '[{"key": "p:t:decision:x", "value": "用 PostgreSQL", "attribute": "decision", "importance": 9, "tier": "pinned"}]'
        candidates = extractor.parse_response(response)
        assert len(candidates) == 1
        assert candidates[0].importance == 9.0
        assert candidates[0].tier == "pinned"

    def test_parse_importance_clamped(self):
        extractor = AutoExtractor()
        response = '[{"key": "p:t:fact:x", "value": "某事实", "importance": 42}]'
        candidates = extractor.parse_response(response)
        assert candidates[0].importance == 10.0

    def test_parse_invalid_tier_falls_back_to_normal(self):
        extractor = AutoExtractor()
        response = '[{"key": "p:t:fact:x", "value": "某事实", "tier": "super"}]'
        candidates = extractor.parse_response(response)
        assert candidates[0].tier == "normal"

    def test_parse_defaults_when_fields_missing(self):
        extractor = AutoExtractor()
        response = '[{"key": "p:t:fact:x", "value": "某事实"}]'
        candidates = extractor.parse_response(response)
        assert candidates[0].importance == 5.0
        assert candidates[0].tier == "normal"

    @pytest.mark.parametrize("tags", [None, "日志", {"kind": "日志"}])
    def test_parse_non_list_tags_normalizes_to_empty(self, tags):
        extractor = AutoExtractor()
        response = json.dumps({"memories": [{
            "key": "SESSION_SUMMARY",
            "value": "本次确认了长期架构约束。",
            "tags": tags,
        }]}, ensure_ascii=False)

        candidates = extractor.parse_response(response)

        assert len(candidates) == 1
        assert candidates[0].tags == []

    def test_parse_invalid_confidence_error_does_not_echo_provider_value(self):
        extractor = AutoExtractor()
        secret = "Synthetic-Pass-In-Confidence-123!"
        response = json.dumps({"memories": [{
            "key": "SESSION_SUMMARY",
            "value": "本次确认了长期架构约束。",
            "confidence": secret,
        }]}, ensure_ascii=False)

        with pytest.raises(ValueError) as exc_info:
            extractor.parse_response(response)

        assert secret not in str(exc_info.value)

    def test_parse_skips_non_string_key_or_value(self):
        extractor = AutoExtractor()
        response = json.dumps({"memories": [
            {
                "key": {"topic": "malformed"},
                "value": "这是键格式错误的候选。",
            },
            {
                "key": "project:test:fact:malformed",
                "value": {"text": "这是值格式错误的候选。"},
            },
        ]}, ensure_ascii=False)

        candidates = extractor.parse_response(response)

        assert candidates == []

    def test_parse_nan_importance_falls_back_to_default(self):
        """json.loads 接受 NaN 字面量；min(10.0, nan) 会返回 10.0，必须回退默认 5.0。"""
        extractor = AutoExtractor()
        response = '[{"key": "p:t:fact:nan", "value": "x", "importance": NaN}]'
        candidates = extractor.parse_response(response)
        assert len(candidates) == 1
        assert candidates[0].importance == 5.0

    def test_should_persist_rejects_overlong_value(self):
        extractor = AutoExtractor()
        c = CandidateMemory(key="p:t:fact:x", value="x" * 501, importance=8.0)
        assert extractor.should_persist(c) is False
        c2 = CandidateMemory(key="p:t:fact:x", value="x" * 200, importance=8.0)
        assert extractor.should_persist(c2) is True

    def test_extraction_prompt_mentions_importance_and_length(self):
        extractor = AutoExtractor()
        prompt = extractor.build_extraction_prompt([{"role": "user", "content": "hi"}])
        assert "importance" in prompt
        assert "200" in prompt

    def test_extraction_prompt_requires_session_summary(self):
        extractor = AutoExtractor()
        prompt = extractor.build_extraction_prompt([{"role": "user", "content": "hi"}])
        assert "SESSION_SUMMARY" in prompt
        assert "即使没有原子记忆也不能省略" in prompt
        assert '{"memories": []}' not in prompt

    def test_extraction_prompt_requests_memories_object_protocol(self):
        extractor = AutoExtractor()

        prompt = extractor.build_extraction_prompt(
            [{"role": "user", "content": "hi"}]
        )

        assert "JSON 对象" in prompt
        assert '"memories"' in prompt
