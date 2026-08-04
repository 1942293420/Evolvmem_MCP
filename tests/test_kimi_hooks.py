"""kimi_hooks module tests (external LLM/IO is replaced at the boundary)."""

import json
import sqlite3
from email.message import Message
from io import BytesIO
from urllib.error import HTTPError

import pytest
from evolvmem.auto_extractor import CandidateMemory
from evolvmem.config import Config
import evolvmem.kimi_hooks as hooks
from evolvmem.kimi_hooks import (
    _canonical_session_id,
    _project_from_wire,
    _split_summary_candidate,
)
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

            def read(self, _size):
                return json.dumps({
                    "choices": [{
                        "message": {"content": '{"memories": []}'},
                    }],
                }).encode("utf-8")

        def fake_open(request, timeout):
            captured["url"] = request.full_url
            captured["authorization"] = request.get_header("Authorization")
            captured["body"] = json.loads(request.data.decode("utf-8"))
            captured["timeout"] = timeout
            return FakeResponse()

        class FakeOpener:
            def open(self, request, timeout):
                return fake_open(request, timeout)

        def fake_build_opener(*handlers):
            captured["redirect_handler"] = any(
                isinstance(handler, hooks._SafeRedirectHandler)
                for handler in handlers
            )
            return FakeOpener()

        monkeypatch.setattr(hooks.urllib.request, "build_opener", fake_build_opener)
        monkeypatch.setattr(
            hooks.urllib.request,
            "urlopen",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("default redirect-capable urlopen must not run")
            ),
        )
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
            "timeout": 120.0,
            "redirect_handler": True,
        }

    def test_kimi_provider_uses_explicit_manual_defaults(
            self, monkeypatch, tmp_path):
        config_path = tmp_path / "llm_credentials.json"
        config_path.write_text(json.dumps({
            "provider": "kimi",
            "api_key": "test-key",
        }), encoding="utf-8")
        monkeypatch.setattr(hooks, "_LLM_CONFIG_PATH", config_path)

        config = hooks._load_llm_config()

        assert config == hooks.LLMConfig(
            provider="kimi",
            api_key="test-key",
            base_url="https://api.kimi.com/coding/v1/chat/completions",
            model="kimi-for-coding",
        )


class TestSafeHTTPBoundary:
    @pytest.mark.parametrize("target", [
        "https://attacker.invalid/chat/completions",
        "http://api.deepseek.com/chat/completions",
    ])
    def test_redirect_handler_rejects_cross_origin_or_https_downgrade(
            self, target):
        request = hooks.urllib.request.Request(
            "https://api.deepseek.com/v1/chat/completions",
            headers={"Authorization": "Bearer test-key"},
        )

        with pytest.raises(HTTPError, match="unsafe redirect blocked"):
            hooks._SafeRedirectHandler().redirect_request(
                request,
                BytesIO(),
                302,
                "redirect",
                Message(),
                target,
            )

    def test_redirect_handler_allows_same_origin_without_dropping_auth(self):
        request = hooks.urllib.request.Request(
            "https://api.deepseek.com/v1/chat/completions",
            headers={"Authorization": "Bearer test-key"},
        )

        redirected = hooks._SafeRedirectHandler().redirect_request(
            request,
            BytesIO(),
            302,
            "redirect",
            Message(),
            "https://api.deepseek.com/v2/chat/completions",
        )

        assert redirected.full_url == (
            "https://api.deepseek.com/v2/chat/completions"
        )
        assert redirected.get_header("Authorization") == "Bearer test-key"

    def test_call_uses_only_remaining_shared_deadline_as_timeout(
            self, monkeypatch):
        captured = {}

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def read(self, size):
                captured["read_size"] = size
                return json.dumps({
                    "choices": [{"message": {"content": "{}"}}],
                }).encode("utf-8")

        class FakeOpener:
            def open(self, _request, timeout):
                captured["timeout"] = timeout
                return FakeResponse()

        monkeypatch.setattr(
            hooks.urllib.request,
            "build_opener",
            lambda *_handlers: FakeOpener(),
        )
        monkeypatch.setattr(hooks.time, "monotonic", lambda: 10.0)

        content = hooks._call_llm("prompt", _llm_config(), deadline=50.0)

        assert content == "{}"
        assert captured == {
            "timeout": 40.0,
            "read_size": hooks._MAX_RESPONSE_BYTES + 1,
        }

    def test_call_rejects_oversized_success_body(self, monkeypatch):
        read_sizes = []

        class OversizedResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def read(self, size):
                read_sizes.append(size)
                return b"x" * size

        class FakeOpener:
            def open(self, _request, timeout):
                return OversizedResponse()

        monkeypatch.setattr(
            hooks.urllib.request,
            "build_opener",
            lambda *_handlers: FakeOpener(),
        )
        monkeypatch.setattr(hooks.time, "monotonic", lambda: 10.0)

        with pytest.raises(
            hooks.RetryableExtractionError,
            match="response body exceeded limit",
        ):
            hooks._call_llm("prompt", _llm_config(), deadline=50.0)

        assert read_sizes == [hooks._MAX_RESPONSE_BYTES + 1]

    def test_call_checks_deadline_after_success_read(self, monkeypatch):
        clock = iter([10.0, 51.0])

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def read(self, _size):
                return json.dumps({
                    "choices": [{"message": {"content": "{}"}}],
                }).encode("utf-8")

        class FakeOpener:
            def open(self, _request, timeout):
                return FakeResponse()

        monkeypatch.setattr(
            hooks.urllib.request,
            "build_opener",
            lambda *_handlers: FakeOpener(),
        )
        monkeypatch.setattr(hooks.time, "monotonic", lambda: next(clock))

        with pytest.raises(
            hooks.RetryableExtractionError,
            match="retry budget exhausted",
        ):
            hooks._call_llm("prompt", _llm_config(), deadline=50.0)

    def test_http_error_body_read_is_bounded_and_deadline_checked(
            self, monkeypatch):
        read_sizes = []

        class RecordingBody(BytesIO):
            def read(self, size=-1):
                read_sizes.append(size)
                return super().read(size)

        error = HTTPError(
            "https://api.deepseek.com/test",
            503,
            "unavailable",
            Message(),
            RecordingBody(b"X" * (128 * 1024)),
        )
        clock = iter([10.0, 51.0])
        monkeypatch.setattr(hooks.time, "monotonic", lambda: next(clock))

        with pytest.raises(
            hooks.RetryableExtractionError,
            match="retry budget exhausted",
        ):
            hooks._http_error_body(error, deadline=50.0)

        assert read_sizes == [hooks._MAX_ERROR_BODY_BYTES]


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

    def test_bounds_local_project_segment_used_in_summary_metadata(self):
        long_name = "project" * 20
        path = (
            f"/home/u/.kimi-code/sessions/wd_{long_name}_1a32444e9dae"
            "/session_x/agents/main/wire.jsonl"
        )

        project = _project_from_wire(path, {})

        assert len(project) <= 48
        assert project == long_name[:48]

    def test_sensitive_local_project_metadata_falls_back_to_general(self):
        secret = "Synthetic-Project-Metadata-Secret"
        path = (
            "/home/u/.kimi-code/sessions/"
            f"wd_DATABASE_PASSWORD={secret}_1a32444e9dae/"
            "session_x/agents/main/wire.jsonl"
        )

        project = _project_from_wire(path, {})

        assert project == "general"
        assert secret not in project

    def test_sensitive_source_session_is_replaced_with_bounded_opaque_id(self):
        secret = "Synthetic-Source-Metadata-Secret"

        session_id = _canonical_session_id(
            f"DATABASE_PASSWORD={secret}",
            "/tmp/wire.jsonl",
        )

        assert session_id.startswith("session_")
        assert len(session_id) <= 128
        assert secret not in session_id


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

        def fake_call(prompt, token, *, deadline=None):
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

        def fake_call(prompt, token, *, deadline=None):
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

        def fake_call(prompt, token, *, deadline=None):
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

        def fake_call(prompt, token, *, deadline=None):
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
            lambda _prompt, _config, **_kwargs: '{"memories": []}',
        )

        with pytest.raises(hooks.RetryableExtractionError):
            hooks._extract_candidates(
                [{"role": "user", "content": "模型返回了空 memories"}],
                _llm_config(),
            )

    def test_timeout_retries_then_returns_valid_extraction(self, monkeypatch):
        attempts = 0

        def fake_call(prompt, token, *, deadline=None):
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

        def fake_call(prompt, token, *, deadline=None):
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

        def fake_call(prompt, token, *, deadline=None):
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

        def fake_call(prompt, token, *, deadline=None):
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

    def test_retry_budget_rejects_success_returned_after_deadline(
            self, monkeypatch):
        clock = iter([0.0, 241.0])
        monkeypatch.setattr(
            hooks,
            "_call_llm",
            lambda *_args, **_kwargs: _extraction_response(
                "超时返回的摘要", "project:test:fact:late"
            ),
        )
        monkeypatch.setattr(hooks.time, "monotonic", lambda: next(clock))

        with pytest.raises(
            hooks.RetryableExtractionError,
            match="retry budget exhausted",
        ):
            hooks._call_llm_with_retry(
                "prompt", _llm_config(), deadline=240.0
            )


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

    def test_session_end_rolls_back_summary_and_atomics_on_third_write_failure(
            self, monkeypatch, tmp_path, test_config):
        self._wire_session(monkeypatch, tmp_path, test_config)
        monkeypatch.setattr(hooks, "_load_llm_config", _llm_config)
        monkeypatch.setattr(hooks, "_extract_candidates", lambda *_: [
            CandidateMemory(
                key="SESSION_SUMMARY",
                value="本次确认了三项长期架构规则。",
                tags=["日志"],
            ),
            CandidateMemory(
                key="project:x:decision:first",
                value="采用第一项长期架构决定。",
            ),
            CandidateMemory(
                key="project:x:decision:second",
                value="采用第二项长期架构决定。",
            ),
            CandidateMemory(
                key="project:x:constraint:third",
                value="必须遵守第三项长期安全约束。",
            ),
        ])
        real_add = MemoryStore.add
        calls = 0

        def fail_on_third(self, *args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 3:
                raise sqlite3.OperationalError(
                    "synthetic third write failure"
                )
            return real_add(self, *args, **kwargs)

        monkeypatch.setattr(MemoryStore, "add", fail_on_third)

        result = hooks.session_end({"session_id": "synthetic"})

        assert result.status == "retry"
        with MemoryStore(test_config) as store:
            assert store.count_active() == 0

    def test_session_end_syncs_vectors_after_commit_and_keeps_completed_on_failure(
            self, monkeypatch, tmp_path, test_config):
        from evolvmem import embedding, vector_index

        self._wire_session(monkeypatch, tmp_path, test_config)
        monkeypatch.setattr(hooks, "_load_llm_config", _llm_config)
        monkeypatch.setattr(
            hooks,
            "_call_llm",
            lambda *_, **__: json.dumps({"memories": [
                {
                    "key": "SESSION_SUMMARY",
                    "value": "本次确认了长期统一接口方案。",
                    "tags": ["日志"],
                },
                {
                    "key": "project:x:decision:api",
                    "value": "采用统一接口，因为它能够长期减少重复实现。",
                    "attribute": "decision",
                },
            ]}, ensure_ascii=False),
        )
        committed_batches = []

        class LoadedEngine:
            is_loaded = True

            def __init__(self, config):
                self.config = config

            def initialize(self):
                pass

            def encode_document(self, value):
                return [0.0, 1.0]

            def close(self):
                pass

        class FailingIndex:
            def __init__(self, config):
                self.config = config

            def initialize(self, dim):
                pass

            def add(self, memory_id, embedding_value):
                connection = sqlite3.connect(str(self.config.db_path))
                try:
                    active_count = connection.execute(
                        "SELECT COUNT(*) FROM memories WHERE status='active'"
                    ).fetchone()[0]
                    row = connection.execute(
                        "SELECT status FROM memories WHERE id=?",
                        (memory_id,),
                    ).fetchone()
                finally:
                    connection.close()
                committed_batches.append((active_count, row[0] if row else None))
                raise RuntimeError("synthetic vector failure")

            def save(self):
                raise AssertionError("save must not run after add failure")

            def close(self):
                pass

        monkeypatch.setattr(embedding, "EmbeddingEngine", LoadedEngine)
        monkeypatch.setattr(vector_index, "VectorIndex", FailingIndex)
        logs = []
        monkeypatch.setattr(hooks, "_log", logs.append)

        result = hooks.session_end({"session_id": "synthetic"})

        assert result.status == "completed"
        assert result.persisted == 2
        assert committed_batches == [(2, "active")]
        with MemoryStore(test_config) as store:
            records = store.get_active()
        assert len(records) == 2
        assert all(record["status"] == "active" for record in records)
        assert "vector sync skipped: RuntimeError" in logs

    def test_session_end_redacts_before_llm_without_mutating_wire(
            self, monkeypatch, tmp_path, test_config):
        secret = "Synthetic-Pass-For-Redaction-123!"
        wire = self._wire_session(
            monkeypatch,
            tmp_path,
            test_config,
            text=f"请分析长期规则。password: {secret}。" + "甲" * 145,
        )
        monkeypatch.setattr(hooks, "_load_llm_config", _llm_config)
        original = wire.read_text(encoding="utf-8")
        assert secret in original
        seen = {}

        def fake_extract(messages, llm_config):
            seen["messages"] = messages
            assert llm_config.provider == "deepseek"
            assert secret not in repr(messages)
            return [CandidateMemory(
                key="SESSION_SUMMARY",
                value="本次确认了长期架构约束并完成安全检查。",
                tags=["日志", "分类:test"],
            )]

        monkeypatch.setattr(hooks, "_extract_candidates", fake_extract)

        result = hooks.session_end({"session_id": "synthetic"})

        assert result.status == "completed"
        assert wire.read_text(encoding="utf-8") == original
        assert "[已脱敏:password]" in repr(seen["messages"])
        assert sum(
            len(message["content"]) for message in seen["messages"]
        ) < 200

    def test_session_end_redacts_before_llm_failure_without_logging_secret(
            self, monkeypatch, tmp_path, test_config):
        secret = "Synthetic-Pass-In-Redaction-Error-123!"
        self._wire_session(monkeypatch, tmp_path, test_config)
        monkeypatch.setattr(hooks, "_load_llm_config", _llm_config)
        monkeypatch.setattr(
            hooks,
            "redact_messages",
            lambda _messages: (_ for _ in ()).throw(ValueError(secret)),
            raising=False,
        )
        monkeypatch.setattr(hooks, "_extract_candidates", lambda *_: [
            CandidateMemory(
                key="SESSION_SUMMARY",
                value="本次确认了长期架构约束并完成安全检查。",
            ),
        ])
        logs = []
        monkeypatch.setattr(hooks, "_log", logs.append)

        result = hooks.session_end({"session_id": "synthetic"})

        assert result.status == "retry"
        assert result.reason == "redaction failed"
        assert "ValueError" in "\n".join(logs)
        assert secret not in "\n".join(logs)

    def test_session_end_malformed_confidence_returns_safe_retry(
            self, monkeypatch, tmp_path, test_config):
        secret = "Synthetic-Pass-In-Provider-Confidence-123!"
        self._wire_session(monkeypatch, tmp_path, test_config)
        monkeypatch.setattr(hooks, "_load_llm_config", _llm_config)
        monkeypatch.setattr(
            hooks,
            "_call_llm",
            lambda *_, **__: json.dumps({"memories": [{
                "key": "SESSION_SUMMARY",
                "value": "本次确认了长期架构约束并完成安全检查。",
                "confidence": secret,
            }]}, ensure_ascii=False),
        )
        logs = []
        monkeypatch.setattr(hooks, "_log", logs.append)

        result = hooks.session_end({"session_id": "synthetic"})

        assert result.status == "retry"
        assert result.reason == "extraction failed"
        assert "ValueError" in "\n".join(logs)
        assert secret not in result.reason
        assert secret not in "\n".join(logs)

    @pytest.mark.parametrize("tags", [None, "日志", {"kind": "日志"}])
    def test_session_end_normalizes_non_list_summary_tags(
            self, monkeypatch, tmp_path, test_config, tags):
        self._wire_session(monkeypatch, tmp_path, test_config)
        monkeypatch.setattr(hooks, "_load_llm_config", _llm_config)
        monkeypatch.setattr(hooks, "_extract_candidates", lambda *_: [
            CandidateMemory(
                key="SESSION_SUMMARY",
                value="本次确认了长期架构约束并完成安全检查。",
                tags=tags,
            ),
        ])

        result = hooks.session_end({"session_id": "synthetic"})

        assert result.status == "completed"
        assert result.persisted == 1
        with MemoryStore(test_config) as store:
            records = store.get_active()
        assert len(records) == 1
        assert records[0]["tags"] == "日志,分类:general"

    def test_session_end_reconstructs_summary_metadata_and_screens_atomics(
            self, monkeypatch, tmp_path, test_config):
        self._wire_session(monkeypatch, tmp_path, test_config)
        monkeypatch.setattr(hooks, "_load_llm_config", _llm_config)
        summary_secret = "Synthetic-Summary-Metadata-Secret"
        atomic_secret = "Synthetic-Atomic-Metadata-Secret"
        monkeypatch.setattr(hooks, "_extract_candidates", lambda *_: [
            CandidateMemory(
                key="SESSION_SUMMARY",
                value="本次确认了长期架构约束并完成安全检查。",
                attribute="constraint",
                tags=[f'password="{summary_secret}"'],
                confidence=1.0,
                importance=10.0,
                tier="pinned",
            ),
            CandidateMemory(
                key="project:test:decision:valid",
                value="采用统一接口，因为它能够长期减少重复实现。",
                attribute="decision",
                tags=["架构"],
                confidence=0.9,
                importance=8.0,
            ),
            CandidateMemory(
                key="project:test:constraint:secret_tag",
                value="这是本应被元数据安全门拒绝的长期约束。",
                attribute="constraint",
                tags=[f'api_key="{atomic_secret}"'],
            ),
            CandidateMemory(
                key="project:test:unknown:attribute",
                value="这是本应被属性模式拒绝的长期事实。",
                attribute="unknown",
            ),
        ])
        logs = []
        monkeypatch.setattr(hooks, "_log", logs.append)

        result = hooks.session_end({"session_id": "synthetic"})

        assert result.status == "completed"
        assert result.persisted == 2
        with MemoryStore(test_config) as store:
            records = store.get_active()
        assert len(records) == 2
        summary = next(
            record for record in records if ":progress:log:" in record["key"]
        )
        assert summary["attribute"] == "fact"
        assert summary["tags"] == "日志,分类:general"
        assert summary["importance"] == 5.0
        assert summary["tier"] == "normal"
        assert summary["source_session"] == "session_synthetic"
        assert all(
            record["source_session"] == "session_synthetic"
            for record in records
        )
        serialized = repr(records) + "\n".join(logs)
        assert summary_secret not in serialized
        assert atomic_secret not in serialized
        stats = next(line for line in logs if "rejected_sensitive=" in line)
        assert "rejected_sensitive=1" in stats
        assert "rejected_metadata=1" in stats

    def test_session_end_retries_when_safe_summary_fails_local_length_gate(
            self, monkeypatch, tmp_path, test_config):
        self._wire_session(monkeypatch, tmp_path, test_config)
        monkeypatch.setattr(hooks, "_load_llm_config", _llm_config)
        monkeypatch.setattr(hooks, "_extract_candidates", lambda *_: [
            CandidateMemory(
                key="SESSION_SUMMARY",
                value="中文摘要。",
                attribute="fact",
            ),
        ])

        result = hooks.session_end({"session_id": "synthetic"})

        assert result.status == "retry"
        assert result.reason == "invalid SESSION_SUMMARY"
        with MemoryStore(test_config) as store:
            assert store.count_active() == 0

    def test_session_end_completes_when_equivalent_summary_already_exists(
            self, monkeypatch, tmp_path, test_config):
        self._wire_session(monkeypatch, tmp_path, test_config)
        monkeypatch.setattr(hooks, "_load_llm_config", _llm_config)
        monkeypatch.setattr(hooks, "_extract_candidates", lambda *_: [
            CandidateMemory(
                key="SESSION_SUMMARY",
                value="本次确认了长期架构约束并完成安全检查。",
                tier="pinned",
            ),
        ])

        first = hooks.session_end({"session_id": "synthetic"})
        second = hooks.session_end({"session_id": "synthetic"})

        assert first.status == "completed"
        assert first.persisted == 1
        assert second.status == "completed"
        assert second.persisted == 0
        with MemoryStore(test_config) as store:
            records = store.get_active()
        assert len(records) == 1
        assert records[0]["tier"] == "normal"

    def test_persist_summary_repairs_equivalent_active_metadata(
            self, test_config):
        key = "project:test:progress:log:2026-08-04-1200"
        value = "本次确认了长期架构约束并完成安全检查。"
        local_summary = CandidateMemory(
            key=key,
            value=value,
            attribute="fact",
            tags=["日志", "分类:test"],
            confidence=1.0,
            importance=5.0,
            tier="normal",
        )
        with MemoryStore(test_config) as store:
            old_id = store.add(
                key,
                value,
                attribute="constraint",
                tags=['password="Synthetic-Legacy-Summary-Metadata"'],
                importance=10.0,
                tier="pinned",
            )

            memory_ids, satisfied = hooks._persist_summary(
                store,
                local_summary,
                "session_synthetic",
            )

            assert satisfied is True
            assert len(memory_ids) == 1
            assert store.get_by_id(old_id)["status"] == "superseded"
            active = store.get_by_id(memory_ids[0])
        assert active["attribute"] == "fact"
        assert active["tags"] == "日志,分类:test"
        assert active["importance"] == 5.0
        assert active["tier"] == "normal"

    def test_session_end_filters_before_dedupe_and_fills_actual_write_quota(
            self, monkeypatch, tmp_path, test_config):
        self._wire_session(monkeypatch, tmp_path, test_config)
        monkeypatch.setattr(hooks, "_load_llm_config", _llm_config)
        existing_key = "project:test:decision:existing"
        existing_value = "采用现有方案，因为它能够长期避免重复写入。"
        with MemoryStore(test_config) as store:
            store.add(
                existing_key,
                existing_value,
                attribute="decision",
                tier="pinned",
            )

        candidates = [
            CandidateMemory(
                key="SESSION_SUMMARY",
                value="本次确认了多项长期架构决定和安全约束。",
            ),
            CandidateMemory(
                key="project:test:decision:shared",
                value="甲" * 501,
                attribute="decision",
                tier="pinned",
                importance=10,
                confidence=1.0,
            ),
            CandidateMemory(
                key="PROJECT:TEST:DECISION:SHARED",
                value="采用共享方案，因为它能够长期减少重复维护。",
                attribute="decision",
                importance=8,
                confidence=0.9,
            ),
            CandidateMemory(
                key=existing_key,
                value=existing_value,
                attribute="decision",
                tier="pinned",
                importance=10,
                confidence=1.0,
            ),
            CandidateMemory(
                key="project:test:decision:low_confidence",
                value="这是不应占据配额的长期低置信度决定。",
                attribute="decision",
                tier="pinned",
                confidence=0.1,
            ),
            CandidateMemory(
                key="project:test:invalid:attribute",
                value="这是不应占据配额的长期无效属性记录。",
                attribute="invalid",
                tier="pinned",
            ),
            CandidateMemory(
                key="project:test:fact:low_information",
                value="会话继续，后续处理。",
                tier="pinned",
            ),
        ]
        candidates.extend(
            CandidateMemory(
                key=f"project:test:decision:valid_{index}",
                value=f"采用第{index}项长期架构决定，因为它能够减少维护成本。",
                attribute="decision",
                importance=8 - index / 100,
                confidence=0.9,
            )
            for index in range(10)
        )
        monkeypatch.setattr(
            hooks,
            "_extract_candidates",
            lambda *_: candidates,
        )

        result = hooks.session_end({"session_id": "synthetic"})

        assert result.status == "completed"
        assert result.persisted == 9
        with MemoryStore(test_config) as store:
            records = store.get_active()
        newly_written_atomics = [
            record for record in records
            if record["source_session"] == "session_synthetic"
            and ":progress:log:" not in record["key"]
        ]
        assert len(newly_written_atomics) == 8
        assert any(
            record["key"] == "project:test:decision:shared"
            for record in newly_written_atomics
        )
        assert all(len(record["value"]) <= 500 for record in records)

    def test_session_end_malformed_key_returns_safe_retry(
            self, monkeypatch, tmp_path, test_config):
        secret = "Synthetic-Pass-In-Malformed-Key-123!"
        self._wire_session(monkeypatch, tmp_path, test_config)
        monkeypatch.setattr(hooks, "_load_llm_config", _llm_config)
        monkeypatch.setattr(hooks, "_extract_candidates", lambda *_: [
            CandidateMemory(
                key={"secret": secret},
                value="这是格式错误但不应逃逸的长期候选。",
            ),
            CandidateMemory(
                key="SESSION_SUMMARY",
                value="本次确认了长期架构约束并完成安全检查。",
            ),
        ])
        logs = []
        monkeypatch.setattr(hooks, "_log", logs.append)

        result = hooks.session_end({"session_id": "synthetic"})

        assert result.status == "retry"
        assert result.reason == "candidate policy failed"
        assert "AttributeError" in "\n".join(logs)
        assert secret not in result.reason
        assert secret not in "\n".join(logs)

    @pytest.mark.parametrize("summary_value", [
        "English-only session summary",
        "password: Synthetic-Pass-Only-123!",
    ])
    def test_session_end_retries_without_writes_when_summary_is_invalid(
            self, monkeypatch, tmp_path, test_config, summary_value):
        self._wire_session(monkeypatch, tmp_path, test_config)
        monkeypatch.setattr(hooks, "_load_llm_config", _llm_config)
        config_loads = []
        monkeypatch.setattr(
            Config,
            "from_file",
            classmethod(
                lambda cls, path=None: config_loads.append(path) or test_config
            ),
        )
        monkeypatch.setattr(hooks, "_extract_candidates", lambda *_: [
            CandidateMemory(key="SESSION_SUMMARY", value=summary_value),
            CandidateMemory(
                key="project:x:constraint:safe",
                value="这是应当保留的长期安全约束。",
                attribute="constraint",
            ),
        ])

        result = hooks.session_end({"session_id": "synthetic"})

        assert result.status == "retry"
        assert config_loads == []
        with MemoryStore(test_config) as store:
            assert store.count_active() == 0

    @pytest.mark.parametrize(
        "candidates,expected_status,expected_reason,expected_persisted",
        [
            (
                [CandidateMemory(
                    key="SESSION_SUMMARY",
                    value={
                        "secret": "Synthetic-Pass-Malformed-Summary-123!"
                    },
                )],
                "retry",
                "candidate policy failed",
                0,
            ),
            (
                [
            CandidateMemory(
                key="SESSION_SUMMARY",
                value="本次确认了长期架构约束并完成安全检查。",
            ),
            CandidateMemory(
                key="project:test:fact:malformed",
                value={"secret": "Synthetic-Pass-Malformed-Candidate-123!"},
            ),
                ],
                "completed",
                "",
                1,
            ),
        ],
        ids=["summary", "ordinary"],
    )
    def test_session_end_handles_malformed_candidate_without_leaking(
            self, monkeypatch, tmp_path, test_config, candidates,
            expected_status, expected_reason, expected_persisted):
        self._wire_session(monkeypatch, tmp_path, test_config)
        monkeypatch.setattr(hooks, "_load_llm_config", _llm_config)
        monkeypatch.setattr(
            hooks, "_extract_candidates", lambda *_: candidates,
        )
        logs = []
        monkeypatch.setattr(hooks, "_log", logs.append)

        result = hooks.session_end({"session_id": "synthetic"})

        assert result.status == expected_status
        assert result.reason == expected_reason
        assert result.persisted == expected_persisted
        if expected_status == "retry":
            assert "AttributeError" in "\n".join(logs)
        else:
            assert "rejected_metadata=1" in "\n".join(logs)
        assert "Synthetic-Pass" not in "\n".join(logs)
        with MemoryStore(test_config) as store:
            assert store.count_active() == expected_persisted

    def test_session_end_pinned_sorting_filters_candidates_and_keeps_quota(
            self, monkeypatch, tmp_path, test_config):
        self._wire_session(monkeypatch, tmp_path, test_config)
        monkeypatch.setattr(hooks, "_load_llm_config", _llm_config)
        candidates = [
            CandidateMemory(
                key=f"project:test:decision:durable_{index}",
                value=f"这是需要长期保留的架构决定第{index}条。",
                attribute="decision",
                importance=8 - index / 10,
                confidence=0.9,
            )
            for index in range(9)
        ]
        candidates.extend([
            CandidateMemory(
                key="project:test:constraint:credential",
                value="长期密码是 password: Synthetic-Pass-Reject-123!",
                attribute="constraint",
            ),
            CandidateMemory(
                key="project:test:fact:one_off_test",
                value="本次测试已经成功完成。",
            ),
            CandidateMemory(
                key="project:test:fact:english_only",
                value="English-only durable candidate",
            ),
            CandidateMemory(
                key="user:preference:communication:language",
                value="用户长期偏好使用中文沟通。",
                attribute="preference",
                tier="pinned",
                importance=5,
                confidence=0.8,
            ),
            CandidateMemory(
                key="SESSION_SUMMARY",
                value="本次确认了长期架构约束并完成安全检查。",
                tags=["日志", "分类:test"],
            ),
        ])
        monkeypatch.setattr(
            hooks, "_extract_candidates", lambda *_: candidates,
        )
        logs = []
        monkeypatch.setattr(hooks, "_log", logs.append)

        result = hooks.session_end({"session_id": "synthetic"})

        assert result.status == "completed"
        assert result.persisted == 9
        with MemoryStore(test_config) as store:
            records = store.get_active()
        assert len(records) == 9
        summaries = [
            record for record in records if ":progress:log:" in record["key"]
        ]
        atomics = [
            record for record in records if ":progress:log:" not in record["key"]
        ]
        assert len(summaries) == 1
        assert len(atomics) == 8
        assert any(
            record["key"] == "user:preference:communication:language"
            for record in atomics
        )
        assert all(
            "Synthetic-Pass" not in record["value"] for record in records
        )
        stats_line = next(line for line in logs if "rejected_sensitive=" in line)
        assert stats_line == (
            "provider=deepseek redacted=0 accepted=8 "
            "rejected_sensitive=1 rejected_ephemeral=1 "
            "rejected_language=1 rejected_metadata=0 "
            "rejected_confidence=0 rejected_length=0 "
            "rejected_low_information=0 persisted=9"
        )
        assert "Synthetic-Pass" not in "\n".join(logs)

    def test_persist_candidates_stops_after_eight_actual_writes(
            self, test_config):
        existing_key = "project:test:decision:existing"
        existing_value = "采用既有长期决定，因为它能够减少重复写入。"
        candidates = [CandidateMemory(
            key=existing_key,
            value=existing_value,
            attribute="decision",
            tier="pinned",
        )]
        candidates.extend([
            CandidateMemory(
                key=f"project:test:decision:uncapped_{index}",
                value=f"这是调用方已经筛选完成的长期决定第{index}条。",
                attribute="decision",
            )
            for index in range(9)
        ])

        with MemoryStore(test_config) as store:
            store.add(existing_key, existing_value, attribute="decision")
            memory_ids = hooks._persist_candidates(
                test_config, store, None, None, candidates, "synthetic",
                max_writes=8,
            )
            records = store.get_active()

        assert memory_ids == list(range(2, 10))
        assert len(records) == 9

    def test_persist_candidates_preserves_source_session_on_replace_paths(
            self, monkeypatch, test_config):
        from evolvmem import semantic_merge

        same_key = "project:test:decision:api"
        semantic_key = "project:test:decision:storage"
        with MemoryStore(test_config) as store:
            old_same_key_id = store.add(
                same_key,
                "采用旧接口。",
                source_session="old-session",
            )
            old_semantic_id = store.add(
                semantic_key,
                "采用旧存储方案。",
                source_session="old-session",
            )
            monkeypatch.setattr(
                semantic_merge,
                "find_semantic_match",
                lambda *_args, **_kwargs: store.get_by_id(old_semantic_id),
            )

            same_key_ids = hooks._persist_candidates(
                test_config,
                store,
                None,
                None,
                [CandidateMemory(
                    key=same_key,
                    value="采用统一接口，因为它能够长期减少重复实现。",
                )],
                "replacement-session",
            )
            semantic_ids = hooks._persist_candidates(
                test_config,
                store,
                object(),
                type("LoadedEngine", (), {"is_loaded": True})(),
                [CandidateMemory(
                    key="project:test:fact:storage-alias",
                    value="采用统一存储方案，因为它能够长期减少维护成本。",
                )],
                "semantic-session",
            )

            assert store.get_by_id(old_same_key_id)["status"] == "superseded"
            assert store.get_by_id(same_key_ids[0])["source_session"] == (
                "replacement-session"
            )
            assert store.get_by_id(old_semantic_id)["status"] == "superseded"
            assert store.get_by_id(semantic_ids[0])["source_session"] == (
                "semantic-session"
            )

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
