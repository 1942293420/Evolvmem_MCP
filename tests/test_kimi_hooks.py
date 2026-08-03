"""kimi_hooks module tests (external LLM/IO is replaced at the boundary)."""

import json
from email.message import Message
from io import BytesIO
from urllib.error import HTTPError

import pytest
from evolvmem.auto_extractor import CandidateMemory
from evolvmem.config import Config
import evolvmem.kimi_hooks as hooks
from evolvmem.kimi_hooks import _project_from_wire, _split_summary_candidate
from evolvmem.memory_store import MemoryStore


def _extraction_response(summary: str, fact_key: str) -> str:
    return json.dumps({"memories": [
        {
            "key": fact_key,
            "value": f"值得长期保存的事实：{fact_key}",
            "attribute": "fact",
            "tags": [],
            "confidence": 0.9,
            "importance": 6,
            "tier": "normal",
        },
        {
            "key": "SESSION_SUMMARY",
            "value": summary,
            "attribute": "fact",
            "tags": ["日志"],
            "confidence": 0.9,
            "importance": 5,
            "tier": "normal",
        },
    ]}, ensure_ascii=False)


def _http_error(code: int, body: str = "{}", retry_after: str | None = None):
    headers = Message()
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return HTTPError(
        "https://api.kimi.com/test", code, "request failed", headers,
        BytesIO(body.encode("utf-8")),
    )


def _llm_config():
    return hooks.LLMConfig(
        provider="deepseek",
        api_key="test-key",
        base_url="https://api.deepseek.com/chat/completions",
        model="deepseek-v4-flash",
    )


def _write_wire(tmp_path, user_text: str, assistant_text: str = "处理完成"):
    wire = tmp_path / "wire.jsonl"
    events = [
        {
            "type": "turn.prompt",
            "input": [{"type": "text", "text": user_text}],
        },
        {
            "type": "context.append_loop_event",
            "event": {
                "type": "content.part",
                "part": {"type": "text", "text": assistant_text},
            },
        },
    ]
    wire.write_text(
        "\n".join(json.dumps(event, ensure_ascii=False) for event in events),
        encoding="utf-8",
    )
    return wire


class TestLLMConfig:
    def test_loads_deepseek_credentials_file(self, monkeypatch, tmp_path):
        config_path = tmp_path / "llm_credentials.json"
        config_path.write_text(json.dumps({
            "provider": "deepseek",
            "api_key": "test-key",
            "base_url": "https://api.deepseek.com/chat/completions",
            "model": "deepseek-v4-flash",
        }), encoding="utf-8")
        monkeypatch.setattr(hooks, "_LLM_CONFIG_PATH", config_path)

        config = hooks._load_llm_config()

        assert config == hooks.LLMConfig(
            provider="deepseek",
            api_key="test-key",
            base_url="https://api.deepseek.com/chat/completions",
            model="deepseek-v4-flash",
        )

    def test_deepseek_request_uses_json_object_non_thinking_mode(
            self, monkeypatch):
        captured = {}

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def read(self):
                return json.dumps({
                    "choices": [{
                        "message": {"content": '{"memories": []}'},
                    }],
                }).encode("utf-8")

        def fake_urlopen(request, timeout):
            captured["url"] = request.full_url
            captured["authorization"] = request.get_header("Authorization")
            captured["body"] = json.loads(request.data.decode("utf-8"))
            captured["timeout"] = timeout
            return FakeResponse()

        monkeypatch.setattr(hooks.urllib.request, "urlopen", fake_urlopen)
        config = hooks.LLMConfig(
            provider="deepseek",
            api_key="test-key",
            base_url="https://api.deepseek.com/chat/completions",
            model="deepseek-v4-flash",
        )

        content = hooks._call_llm("extract this conversation", config)

        assert content == '{"memories": []}'
        assert captured == {
            "url": "https://api.deepseek.com/chat/completions",
            "authorization": "Bearer test-key",
            "body": {
                "model": "deepseek-v4-flash",
                "messages": [{
                    "role": "user",
                    "content": "extract this conversation",
                }],
                "max_tokens": 4096,
                "temperature": 0.2,
                "thinking": {"type": "disabled"},
                "response_format": {"type": "json_object"},
            },
            "timeout": 120,
        }


class TestProjectFromWire:
    def test_alias_mapping(self):
        path = ("/home/u/.kimi-code/sessions/wd_automation-control_1a32444e9dae"
                "/session_x/agents/main/wire.jsonl")
        aliases = {"automation-control": "automation_control"}
        assert _project_from_wire(path, aliases) == "automation_control"

    def test_no_alias_uses_dirname(self):
        path = ("/home/u/.kimi-code/sessions/wd_eva_1a32444e9dae"
                "/session_x/agents/main/wire.jsonl")
        assert _project_from_wire(path, {}) == "eva"

    def test_unrecognized_path_falls_back_to_general(self):
        assert _project_from_wire("/tmp/wire.jsonl", {}) == "general"

    def test_sanitizes_dirname(self):
        path = ("/home/u/.kimi-code/sessions/wd_My Shop_1a32444e9dae"
                "/session_x/agents/main/wire.jsonl")
        assert _project_from_wire(path, {}) == "my_shop"


class TestSplitSummaryCandidate:
    def test_pulls_out_summary(self):
        summary = CandidateMemory(key="SESSION_SUMMARY", value="本次做了 X")
        other = CandidateMemory(key="p:t:fact:x", value="某事实")
        s, rest = _split_summary_candidate([other, summary])
        assert s is summary
        assert rest == [other]

    def test_no_summary_returns_all(self):
        other = CandidateMemory(key="p:t:fact:x", value="某事实")
        s, rest = _split_summary_candidate([other])
        assert s is None
        assert rest == [other]

    def test_case_insensitive_key_match(self):
        summary = CandidateMemory(key="session_summary", value="本次做了 X")
        s, rest = _split_summary_candidate([summary])
        assert s is summary
        assert rest == []


class TestFullConversationExtraction:
    def test_45k_conversation_uses_one_full_request(self, monkeypatch):
        messages = [{"role": "user", "content": "甲" * 45_000}]
        prompts = []

        def fake_call(prompt, token):
            prompts.append(prompt)
            return _extraction_response("完整会话摘要", "project:test:fact:full")

        monkeypatch.setattr(hooks, "_call_llm", fake_call)

        candidates = hooks._extract_candidates(messages, _llm_config())

        assert len(prompts) == 1
        assert "甲" * 45_000 in prompts[0]
        assert [c.key for c in candidates] == [
            "project:test:fact:full", "SESSION_SUMMARY",
        ]

    @pytest.mark.parametrize("status_code", [400, 422])
    def test_context_overflow_falls_back_on_message_boundaries(
            self, monkeypatch, status_code):
        messages = [
            {"role": "user", "content": "第一条消息" * 8},
            {"role": "assistant", "content": "第二条消息" * 8},
        ]
        prompts = []

        def fake_call(prompt, token):
            prompts.append(prompt)
            if len(prompts) == 1:
                raise _http_error(
                    status_code,
                    '{"error":{"code":"context_length_exceeded"}}',
                )
            index = len(prompts) - 1
            return _extraction_response(
                f"第{index}块摘要", f"project:test:fact:chunk{index}"
            )

        monkeypatch.setattr(hooks, "_call_llm", fake_call)

        candidates = hooks._extract_candidates(
            messages, _llm_config(), fallback_chunk_chars=45
        )

        assert len(prompts) == 3  # 一次全量失败，再按两条完整消息降级
        assert messages[0]["content"] in prompts[1]
        assert messages[0]["content"] not in prompts[2]
        assert messages[1]["content"] in prompts[2]
        assert messages[1]["content"] not in prompts[1]
        summaries = [c for c in candidates if c.key == "SESSION_SUMMARY"]
        assert [c.value for c in summaries] == ["第2块摘要"]
        assert [c.key for c in candidates if c.key != "SESSION_SUMMARY"] == [
            "project:test:fact:chunk1", "project:test:fact:chunk2",
        ]


class TestExtractionRetries:
    def test_429_retries_then_returns_valid_extraction(self, monkeypatch):
        attempts = 0

        def fake_call(prompt, token):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise _http_error(429, retry_after="0")
            return _extraction_response(
                "限流后成功摘要", "project:test:fact:retried"
            )

        monkeypatch.setattr(hooks, "_call_llm", fake_call)
        monkeypatch.setattr(hooks.time, "sleep", lambda _seconds: None)

        candidates = hooks._extract_candidates(
            [{"role": "user", "content": "需要提炼的完整会话"}],
            _llm_config(),
        )

        assert attempts == 2
        assert candidates[-1].value == "限流后成功摘要"

    def test_exhausted_429_is_reported_as_rate_limited(self, monkeypatch):
        attempts = 0

        def fake_call(prompt, token):
            nonlocal attempts
            attempts += 1
            raise _http_error(429, retry_after="0")

        monkeypatch.setattr(hooks, "_call_llm", fake_call)
        monkeypatch.setattr(hooks.time, "sleep", lambda _seconds: None)

        with pytest.raises(hooks.RetryableExtractionError) as exc_info:
            hooks._extract_candidates(
                [{"role": "user", "content": "持续被限流的完整会话"}],
                _llm_config(),
            )

        assert attempts == 3
        assert exc_info.value.rate_limited is True

    def test_missing_required_summary_is_retryable(self, monkeypatch):
        monkeypatch.setattr(
            hooks,
            "_call_llm",
            lambda _prompt, _config: '{"memories": []}',
        )

        with pytest.raises(hooks.RetryableExtractionError):
            hooks._extract_candidates(
                [{"role": "user", "content": "模型返回了空 memories"}],
                _llm_config(),
            )

    def test_timeout_retries_then_returns_valid_extraction(self, monkeypatch):
        attempts = 0

        def fake_call(prompt, token):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise TimeoutError("read timed out")
            return _extraction_response(
                "超时后成功摘要", "project:test:fact:timeout"
            )

        monkeypatch.setattr(hooks, "_call_llm", fake_call)
        monkeypatch.setattr(hooks.time, "sleep", lambda _seconds: None)

        candidates = hooks._extract_candidates(
            [{"role": "user", "content": "第一次请求发生读取超时"}],
            _llm_config(),
        )

        assert attempts == 2
        assert candidates[-1].value == "超时后成功摘要"

    def test_503_retries_then_returns_valid_extraction(self, monkeypatch):
        attempts = 0

        def fake_call(prompt, token):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise _http_error(503)
            return _extraction_response(
                "服务恢复摘要", "project:test:fact:service"
            )

        monkeypatch.setattr(hooks, "_call_llm", fake_call)
        monkeypatch.setattr(hooks.time, "sleep", lambda _seconds: None)

        candidates = hooks._extract_candidates(
            [{"role": "user", "content": "服务端短暂不可用"}],
            _llm_config(),
        )

        assert attempts == 2
        assert candidates[-1].value == "服务恢复摘要"

    def test_auth_error_is_deferred_without_retry_loop(self, monkeypatch):
        attempts = 0

        def fake_call(prompt, token):
            nonlocal attempts
            attempts += 1
            raise _http_error(401)

        monkeypatch.setattr(hooks, "_call_llm", fake_call)

        with pytest.raises(hooks.RetryableExtractionError) as exc_info:
            hooks._extract_candidates(
                [{"role": "user", "content": "认证已经失效"}],
                _llm_config(),
            )

        assert attempts == 1
        assert exc_info.value.rate_limited is False

    def test_retry_budget_prevents_another_request(self, monkeypatch):
        attempts = 0

        def fake_call(prompt, token):
            nonlocal attempts
            attempts += 1
            raise _http_error(503)

        clock = iter([0.0, 241.0])
        monkeypatch.setattr(hooks, "_call_llm", fake_call)
        monkeypatch.setattr(hooks.time, "monotonic", lambda: next(clock))
        monkeypatch.setattr(hooks.time, "sleep", lambda _seconds: None)

        with pytest.raises(hooks.RetryableExtractionError):
            hooks._call_llm_with_retry(
                "prompt", _llm_config(), deadline=240.0
            )

        assert attempts == 1


class TestSessionEndOutcome:
    @staticmethod
    def _wire_session(monkeypatch, tmp_path, test_config, text="甲" * 250):
        wire = _write_wire(tmp_path, text)
        monkeypatch.setattr(hooks, "_find_wire", lambda _session_id: str(wire))
        monkeypatch.setattr(
            Config,
            "from_file",
            classmethod(lambda cls, path=None: test_config),
        )
        return wire

    def test_short_conversation_is_explicitly_skipped(
            self, monkeypatch, tmp_path, test_config):
        self._wire_session(monkeypatch, tmp_path, test_config, text="太短")

        result = hooks.session_end({"session_id": "session_short"})

        assert result.status == "skipped"
        assert result.persisted == 0

    def test_missing_provider_config_remains_retryable(
            self, monkeypatch, tmp_path, test_config):
        self._wire_session(monkeypatch, tmp_path, test_config)
        monkeypatch.setattr(hooks, "_load_llm_config", lambda: None)

        result = hooks.session_end({"session_id": "session_token"})

        assert result.status == "retry"
        assert result.persisted == 0

    def test_retryable_extraction_writes_nothing(
            self, monkeypatch, tmp_path, test_config):
        self._wire_session(monkeypatch, tmp_path, test_config)
        monkeypatch.setattr(hooks, "_load_llm_config", _llm_config)
        monkeypatch.setattr(
            hooks,
            "_extract_candidates",
            lambda _messages, _token: (_ for _ in ()).throw(
                hooks.RetryableExtractionError(
                    "still rate limited", rate_limited=True
                )
            ),
        )

        result = hooks.session_end({"session_id": "session_retry"})

        assert result.status == "retry"
        assert result.rate_limited is True
        with MemoryStore(test_config) as store:
            assert store.count_active() == 0

    def test_completed_extraction_returns_count_and_persists(
            self, monkeypatch, tmp_path, test_config):
        self._wire_session(monkeypatch, tmp_path, test_config)
        monkeypatch.setattr(hooks, "_load_llm_config", _llm_config)
        monkeypatch.setattr(
            hooks,
            "_extract_candidates",
            lambda _messages, _token: [
                CandidateMemory(
                    key="project:test:fact:completed",
                    value="这是成功提炼并持久化的长期事实",
                    confidence=0.9,
                ),
                CandidateMemory(
                    key="SESSION_SUMMARY",
                    value="本次会话已经成功完成可靠性测试",
                    confidence=0.9,
                    tags=["日志"],
                ),
            ],
        )

        result = hooks.session_end({"session_id": "session_completed"})

        assert result.status == "completed"
        assert result.persisted == 2
        with MemoryStore(test_config) as store:
            assert store.count_active() == 2
