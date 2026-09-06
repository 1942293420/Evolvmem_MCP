"""kimi_hooks module tests (external LLM/IO is replaced at the boundary)."""

import io
import json
import os
import sqlite3
import subprocess
import sys
from email.message import Message
from io import BytesIO
from pathlib import Path
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
    @pytest.mark.parametrize("custom_data_dir", [False, True], ids=["default", "override"])
    def test_runtime_directory_routes_credentials_log_and_heartbeat(
            self, tmp_path, custom_data_dir):
        home = tmp_path / "home"
        data_dir = home / "custom-data" if custom_data_dir else home / ".claude" / "evolvmem"
        data_dir.mkdir(parents=True)
        (data_dir / "llm_credentials.json").write_text(json.dumps({
            "provider": "kimi",
            "api_key": "test-only-key",
            "model": "test-only-model",
        }), encoding="utf-8")
        repository = Path(__file__).resolve().parents[1]
        env = {
            key: value for key, value in os.environ.items()
            if not key.startswith("EVOLVMEM_")
        }
        env.update({
            "HOME": str(home),
            "PYTHONPATH": str(repository),
            "PYTHONDONTWRITEBYTECODE": "1",
        })
        if custom_data_dir:
            env["EVOLVMEM_DATA_DIR"] = "~/custom-data"
        probe = subprocess.run(
            [sys.executable, "-c", """
from evolvmem import kimi_hooks

config = kimi_hooks._load_llm_config(log_errors=False)
assert config is not None, "fake credentials were not found in the data directory"
assert config.provider == "kimi"
assert config.api_key == "test-only-key"
assert config.model == "test-only-model"
kimi_hooks._log("isolated-path-probe")
kimi_hooks._touch_heartbeat("test-session")
"""],
            cwd=repository, env=env, capture_output=True, text=True, timeout=20,
        )

        assert probe.returncode == 0, probe.stderr
        assert "isolated-path-probe" in (data_dir / "hooks.log").read_text()
        assert (data_dir / "live" / "test-session").is_file()
        if custom_data_dir:
            assert not (home / ".claude").exists()

    def test_load_llm_callable_adapts_existing_retrying_chat(self, monkeypatch):
        config = _llm_config()
        monkeypatch.setattr(hooks, "_load_llm_config", lambda: config)
        calls = []
        monkeypatch.setattr(
            hooks,
            "_call_llm_with_retry",
            lambda prompt, actual: calls.append((prompt, actual)) or "模型结果",
        )

        llm = hooks._load_llm_callable()

        assert callable(llm)
        assert llm("滚动摘要") == "模型结果"
        assert calls == [("滚动摘要", config)]

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
            if len(prompts) == 4:
                return _extraction_response(
                    "总摘要覆盖第一块与第二块", "project:test:fact:synthesis"
                )
            index = len(prompts) - 1
            return _extraction_response(
                f"第{index}块摘要", f"project:test:fact:chunk{index}"
            )

        monkeypatch.setattr(hooks, "_call_llm", fake_call)

        candidates = hooks._extract_candidates(
            messages, _llm_config(), fallback_chunk_chars=45
        )

        assert len(prompts) == 4  # 全量失败、两块提取、一次总摘要合成
        assert messages[0]["content"] in prompts[1]
        assert messages[0]["content"] not in prompts[2]
        assert messages[1]["content"] in prompts[2]
        assert messages[1]["content"] not in prompts[1]
        assert "第1块摘要" in prompts[3]
        assert "第2块摘要" in prompts[3]
        summaries = [c for c in candidates if c.key == "SESSION_SUMMARY"]
        assert [c.value for c in summaries] == ["总摘要覆盖第一块与第二块"]
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
        from evolvmem import context_service, embedding

        test_config.embedding_dim = 2  # LoadedEngine 的 [0.0, 1.0] 与契约维度一致
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
        preserve_calls = []

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
            def __init__(self, config, path=None):
                self.config = config
                self.path = path

            def initialize(self, dim=512):
                pass

            def count(self):
                return 0

            def is_dirty(self):
                return False

            def mark_dirty(self):
                pass

            def preserve_dirty(self):
                preserve_calls.append("preserve_dirty")

            def search(self, embedding_value, k):
                return []

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

            def remove(self, memory_id):
                return False

            def save(self):
                raise AssertionError("save must not run after add failure")

            def close(self):
                pass

        monkeypatch.setattr(embedding, "EmbeddingEngine", LoadedEngine)
        monkeypatch.setattr(context_service, "VectorIndex", FailingIndex)
        logs = []
        monkeypatch.setattr(hooks, "_log", logs.append)

        result = hooks.session_end({"session_id": "synthetic"})

        assert result.status == "completed"
        assert result.persisted == 2
        assert committed_batches == [(2, "active")]  # 向量写入发生在提交之后
        assert preserve_calls  # 失败留下独立 dirty 重试标记，而非假回滚
        with MemoryStore(test_config) as store:
            records = store.get_active()
        assert len(records) == 2
        assert all(record["status"] == "active" for record in records)

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

    def test_session_end_compat_mode_writes_projection_and_core_atomically(
            self, monkeypatch, tmp_path, test_config):
        from evolvmem.context_models import ContextStatus
        from evolvmem.context_store import ContextStore

        test_config.context_mode = "compat"
        self._wire_session(monkeypatch, tmp_path, test_config)
        # compat 模式以既有 legacy 库为前提（正式切换在迁移后才开启）
        with MemoryStore(test_config):
            pass
        monkeypatch.setattr(hooks, "_load_llm_config", _llm_config)
        monkeypatch.setattr(
            hooks,
            "_extract_candidates",
            lambda _messages, _token: [
                CandidateMemory(
                    key="project:test:decision:dual",
                    value="采用双侧原子写入，因为它能够长期保持一致。",
                    attribute="decision",
                    confidence=0.9,
                    importance=8.0,
                ),
                CandidateMemory(
                    key="SESSION_SUMMARY",
                    value="本次确认了双侧原子写入的长期价值。",
                    confidence=0.9,
                    tags=["日志"],
                ),
            ],
        )

        result = hooks.session_end({"session_id": "session_dual"})

        assert result.status == "completed"
        assert result.persisted == 2
        with MemoryStore(test_config) as store:
            records = store.get_active()
        assert len(records) == 2
        with ContextStore(test_config) as context_store:
            for record in records:
                context_id = context_store.resolve_legacy_mapping(record["id"])
                assert context_id is not None
                item = context_store.get_item(context_id)
                assert item.status is ContextStatus.ACTIVE
                assert item.layers is not None  # L0/L1/L2 三层齐备
                assert item.layers.l1 == record["value"]
                assert item.layers.l2 == record["value"]
                if record["key"] == "project:test:decision:dual":
                    assert item.confidence == 0.9
                    assert item.importance == 8.0

    def test_session_end_compat_mode_rolls_back_both_sides_on_write_failure(
            self, monkeypatch, tmp_path, test_config):
        test_config.context_mode = "compat"
        self._wire_session(monkeypatch, tmp_path, test_config)
        # compat 模式以既有 legacy 库为前提（正式切换在迁移后才开启）
        with MemoryStore(test_config):
            pass
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
        from evolvmem.context_store import ContextStore
        from evolvmem.legacy_projection import LegacyProjectionRepository
        real_insert = LegacyProjectionRepository.insert
        calls = 0

        def fail_on_third(self, *args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 3:
                raise sqlite3.OperationalError(
                    "synthetic third write failure"
                )
            return real_insert(self, *args, **kwargs)

        monkeypatch.setattr(
            LegacyProjectionRepository, "insert", fail_on_third
        )

        result = hooks.session_end({"session_id": "synthetic"})

        assert result.status == "retry"
        with MemoryStore(test_config) as store:
            assert store.count_active() == 0
        with ContextStore(test_config) as context_store:
            assert context_store.count_by_status() == {}

    def test_session_end_invalid_context_mode_persists_via_legacy_backend(
            self, monkeypatch, tmp_path, test_config):
        """非法 context_mode（如 typo）不得让提炼永远 retry：按 legacy 落库，
        Context 功能 fail-closed，Core 表无写入。"""
        from evolvmem.context_store import ContextStore

        test_config.context_mode = "primray"
        self._wire_session(monkeypatch, tmp_path, test_config)
        monkeypatch.setattr(hooks, "_load_llm_config", _llm_config)
        monkeypatch.setattr(
            hooks,
            "_extract_candidates",
            lambda _messages, _token: [
                CandidateMemory(
                    key="project:test:fact:invalid-mode",
                    value="非法模式也必须持久化的长期事实",
                    confidence=0.9,
                ),
                CandidateMemory(
                    key="SESSION_SUMMARY",
                    value="本次会话验证了非法模式的 legacy 兜底",
                    confidence=0.9,
                    tags=["日志"],
                ),
            ],
        )

        result = hooks.session_end({"session_id": "session_invalid_mode"})

        assert result.status == "completed"
        assert result.persisted == 2
        with MemoryStore(test_config) as store:
            assert store.count_active() == 2
        with ContextStore(test_config) as context_store:
            assert context_store.count_by_status() == {}



class TestExtractionBackoff:
    def test_backoff_delays_grow_exponentially(self, monkeypatch):
        attempts = 0
        delays = []

        def fake_call(prompt, token, *, deadline=None):
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                raise _http_error(429)
            return _extraction_response("退避后成功摘要", "project:p:fact:backoff")

        monkeypatch.setattr(hooks, "_call_llm", fake_call)
        monkeypatch.setattr(hooks.time, "sleep", delays.append)
        monkeypatch.setattr(hooks.random, "uniform", lambda _a, _b: 0.0)

        candidates = hooks._extract_candidates(
            [{"role": "user", "content": "限流后恢复的会话"}],
            _llm_config(),
        )

        assert attempts == 3
        assert delays == [2.0, 4.0]
        assert candidates[-1].value == "退避后成功摘要"

    def test_retry_after_header_overrides_backoff(self, monkeypatch):
        attempts = 0
        delays = []

        def fake_call(prompt, token, *, deadline=None):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise _http_error(429, retry_after="7")
            return _extraction_response("服从RetryAfter摘要", "project:p:fact:ra")

        monkeypatch.setattr(hooks, "_call_llm", fake_call)
        monkeypatch.setattr(hooks.time, "sleep", delays.append)

        candidates = hooks._extract_candidates(
            [{"role": "user", "content": "限流带Retry-After的会话"}],
            _llm_config(),
        )

        assert delays == [7.0]
        assert candidates[-1].value == "服从RetryAfter摘要"

    def test_quota_error_halts_run_without_retry(self, monkeypatch):
        attempts = 0

        def fake_call(prompt, token, *, deadline=None):
            nonlocal attempts
            attempts += 1
            raise _http_error(402)

        monkeypatch.setattr(hooks, "_call_llm", fake_call)

        with pytest.raises(hooks.RetryableExtractionError) as exc_info:
            hooks._extract_candidates(
                [{"role": "user", "content": "账户欠费的会话"}],
                _llm_config(),
            )

        assert attempts == 1
        assert exc_info.value.halt_run is True
        assert exc_info.value.rate_limited is False


class TestHooksLog:
    def test_log_appends_timestamped_line(self, monkeypatch, tmp_path):
        log_path = tmp_path / "hooks.log"
        monkeypatch.setattr(hooks, "_HOOKS_LOG_PATH", log_path)

        hooks._log("测试落盘")

        content = log_path.read_text(encoding="utf-8")
        assert "测试落盘" in content
        assert "[evolvmem]" in content
        assert content[0].isdigit()  # 时间戳在行首

    def test_log_rotates_when_oversized(self, monkeypatch, tmp_path):
        log_path = tmp_path / "hooks.log"
        log_path.write_text("x" * 100, encoding="utf-8")
        monkeypatch.setattr(hooks, "_HOOKS_LOG_PATH", log_path)
        monkeypatch.setattr(hooks, "_HOOKS_LOG_MAX_BYTES", 10)

        hooks._log("轮转后的新行")

        rotated = tmp_path / "hooks.log.1"
        assert rotated.exists()
        assert rotated.read_text(encoding="utf-8") == "x" * 100
        assert "轮转后的新行" in log_path.read_text(encoding="utf-8")

    def test_log_file_failure_still_prints_stderr(
            self, monkeypatch, tmp_path, capsys):
        monkeypatch.setattr(
            hooks, "_HOOKS_LOG_PATH", tmp_path / "nonexistent_dir" / "h.log")

        hooks._log("只到stderr")

        assert "只到stderr" in capsys.readouterr().err


class TestHeartbeat:
    def test_session_start_forwards_workspace_from_hook_payload(
            self, monkeypatch, tmp_path, capsys):
        captured = {}
        monkeypatch.setattr(
            "evolvmem.hooks.get_session_start_block",
            lambda **kwargs: captured.update(kwargs) or "记忆块",
        )
        monkeypatch.setattr(hooks, "_LIVE_DIR", tmp_path / "live")
        monkeypatch.setattr(sys, "argv", ["kimi_hooks", "session-start"])
        monkeypatch.setattr(
            sys,
            "stdin",
            io.StringIO(json.dumps({
                "session_id": "session_workspace",
                "cwd": "/workspace/eva",
            })),
        )

        with pytest.raises(SystemExit) as exc_info:
            hooks.main()

        assert exc_info.value.code == 0
        assert capsys.readouterr().out == "记忆块"
        assert captured == {"workspace_path": "/workspace/eva"}

    def test_touch_heartbeat_creates_marker(self, monkeypatch, tmp_path):
        live_dir = tmp_path / "live"
        monkeypatch.setattr(hooks, "_LIVE_DIR", live_dir)

        hooks._touch_heartbeat("session_abc")
        hooks._touch_heartbeat("")

        assert (live_dir / "session_abc").exists()
        assert len(list(live_dir.iterdir())) == 1

    def test_touch_heartbeat_sanitizes_session_id(
            self, monkeypatch, tmp_path):
        live_dir = tmp_path / "live"
        monkeypatch.setattr(hooks, "_LIVE_DIR", live_dir)

        hooks._touch_heartbeat("../evil/../../x")

        names = [p.name for p in live_dir.iterdir()]
        assert names
        assert all("/" not in name and ".." not in name for name in names)

    def test_heartbeat_subcommand_writes_no_stdout(
            self, monkeypatch, tmp_path, capsys):
        live_dir = tmp_path / "live"
        monkeypatch.setattr(hooks, "_LIVE_DIR", live_dir)
        monkeypatch.setattr(sys, "argv", ["kimi_hooks", "heartbeat"])
        monkeypatch.setattr(
            sys, "stdin", io.StringIO('{"session_id": "session_hb"}'))

        with pytest.raises(SystemExit) as exc_info:
            hooks.main()

        assert exc_info.value.code == 0
        assert capsys.readouterr().out == ""
        assert (live_dir / "session_hb").exists()


class TestSessionEndArchiveLinking:
    """P4b：session_end 加密归档 + 候选隔离 + 来源链接（同一事务）。"""

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

    @staticmethod
    def _candidates():
        return [
            CandidateMemory(
                key="project:proj:experience:stdio-hang",
                value="MCP 握手卡住时先检查 stdin 预读竞争，改为单一读取路径。",
                attribute="experience",
                confidence=0.8,
                importance=7.0,
            ),
            CandidateMemory(
                key="project:proj:decision:archive",
                value="采用本地加密归档保存原始会话，因为它能够长期保护隐私。",
                attribute="decision",
                confidence=0.9,
                importance=8.0,
            ),
            CandidateMemory(
                key="SESSION_SUMMARY",
                value="本次会话验证了归档与候选隔离的接线。",
                confidence=0.9,
                tags=["日志"],
            ),
        ]

    @staticmethod
    def _rows(config, table):
        conn = sqlite3.connect(config.db_path)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def test_session_end_archives_payload_and_links_sources(
            self, monkeypatch, tmp_path, test_config):
        import stat

        from evolvmem.context_models import ContextStatus
        from evolvmem.context_store import ContextStore
        from evolvmem.session_archive import SessionArchiver

        test_config.context_mode = "compat"
        secret = "sk-live-secret-123456"
        self._wire_session(
            monkeypatch, tmp_path, test_config,
            text=f"我的临时 key 是 {secret}，请勿外发。" + "甲" * 250,
        )
        # compat 模式以既有 legacy 库为前提
        with MemoryStore(test_config):
            pass
        monkeypatch.setattr(hooks, "_load_llm_config", _llm_config)
        monkeypatch.setattr(
            hooks, "_extract_candidates", lambda *_: self._candidates(),
        )

        result = hooks.session_end({"session_id": "session_archive_link"})

        assert result.status == "completed"
        assert result.persisted == 3  # summary + decision + 隔离的 experience

        # 加密归档：一行、payload 落盘且零明文、密钥 owner-only
        archives = self._rows(test_config, "session_archives")
        assert len(archives) == 1
        archive = archives[0]
        assert archive["adapter"] == "kimi"
        assert archive["external_session_id"] == "session_archive_link"
        assert archive["state"] == "available"
        payload_file = test_config.data_dir / archive["payload_path"]
        blob = payload_file.read_bytes()
        assert secret.encode("utf-8") not in blob
        key_file = test_config.data_dir / "archive.key"
        assert stat.S_IMODE(key_file.stat().st_mode) == 0o600

        # 解密后是原始消息列表 JSON（脱敏前的本地证据，绝不外发）
        with ContextStore(test_config) as store:
            payload = SessionArchiver(test_config, store).read_payload(
                archive["id"]
            )
        parsed = json.loads(payload)
        assert set(parsed) == {"messages"}
        roles = [message["role"] for message in parsed["messages"]]
        assert roles == ["user", "assistant"]
        assert secret in parsed["messages"][0]["content"]

        # 候选隔离：experience 是 Core candidate，无 legacy 投影行
        items = self._rows(test_config, "context_items")
        by_key = {item["identity_key"]: item for item in items}
        isolated = by_key["project:proj:experience:stdio-hang"]
        assert isolated["status"] == "candidate"
        assert isolated["content_type"] == "experience"
        assert isolated["source_state"] == "available"
        assert isolated["source_count"] == 1
        memories = self._rows(test_config, "memories")
        assert "project:proj:experience:stdio-hang" not in {
            row["key"] for row in memories
        }
        # 其他类型保持 active 双写
        assert by_key["project:proj:decision:archive"]["status"] == (
            ContextStatus.ACTIVE.value
        )
        assert len(memories) == 2  # summary + decision

        # 来源链接随提炼同事务落库：每个写入项一条 session 来源
        sources = self._rows(test_config, "context_sources")
        assert {row["item_id"] for row in sources} == {
            item["id"] for item in items
        }
        for row in sources:
            assert row["archive_id"] == archive["id"]
            assert row["source_kind"] == "session"
            assert row["extraction_version"] == "kimi-extraction-v1"

    def test_session_end_repeat_same_session_adds_no_rows(
            self, monkeypatch, tmp_path, test_config):
        test_config.context_mode = "compat"
        self._wire_session(monkeypatch, tmp_path, test_config)
        with MemoryStore(test_config):
            pass
        monkeypatch.setattr(hooks, "_load_llm_config", _llm_config)
        monkeypatch.setattr(
            hooks, "_extract_candidates", lambda *_: self._candidates(),
        )

        first = hooks.session_end({"session_id": "session_repeat"})
        second = hooks.session_end({"session_id": "session_repeat"})

        assert first.status == "completed" and first.persisted == 3
        assert second.status == "completed" and second.persisted == 0
        assert len(self._rows(test_config, "session_archives")) == 1
        assert len(self._rows(test_config, "context_items")) == 3
        assert len(self._rows(test_config, "context_sources")) == 3

    def test_session_end_without_crypto_backend_persists_without_archive(
            self, monkeypatch, tmp_path, test_config):
        """无加密库：归档返回 None，提炼照常（隔离不生效），零明文落盘。"""
        test_config.context_mode = "compat"
        self._wire_session(monkeypatch, tmp_path, test_config)
        with MemoryStore(test_config):
            pass
        monkeypatch.setattr(hooks, "_load_llm_config", _llm_config)
        monkeypatch.setattr(
            hooks, "_extract_candidates", lambda *_: self._candidates(),
        )
        monkeypatch.setattr("evolvmem.session_archive.AESGCM", None)

        result = hooks.session_end({"session_id": "session_no_crypto"})

        assert result.status == "completed"
        assert result.persisted == 3
        assert self._rows(test_config, "session_archives") == []
        assert self._rows(test_config, "context_sources") == []
        assert not (test_config.data_dir / "session_archives").exists()
        # 无 archive 时 experience 走旧路径：active 双写
        items = self._rows(test_config, "context_items")
        by_key = {item["identity_key"]: item for item in items}
        assert by_key["project:proj:experience:stdio-hang"]["status"] == "active"
        assert len(self._rows(test_config, "memories")) == 3

    def test_session_end_archive_failure_still_persists(
            self, monkeypatch, tmp_path, test_config):
        from evolvmem.session_archive import SessionArchiver

        test_config.context_mode = "compat"
        self._wire_session(monkeypatch, tmp_path, test_config)
        with MemoryStore(test_config):
            pass
        monkeypatch.setattr(hooks, "_load_llm_config", _llm_config)
        monkeypatch.setattr(
            hooks, "_extract_candidates", lambda *_: self._candidates(),
        )

        def boom(*args, **kwargs):
            raise RuntimeError("synthetic archive failure at /secret/path")

        monkeypatch.setattr(SessionArchiver, "archive_session", boom)
        logs = []
        monkeypatch.setattr(hooks, "_log", logs.append)

        result = hooks.session_end({"session_id": "session_archive_fail"})

        assert result.status == "completed"
        assert result.persisted == 3
        assert self._rows(test_config, "session_archives") == []
        assert self._rows(test_config, "context_sources") == []
        assert any("archive" in line for line in logs)
        assert "/secret/path" not in "\n".join(logs)

    def test_session_end_isolates_playbook_candidate(
            self, monkeypatch, tmp_path, test_config):
        """P5 合约矩阵：playbook 与 experience 一样只建 Core candidate。"""
        test_config.context_mode = "compat"
        self._wire_session(monkeypatch, tmp_path, test_config)
        with MemoryStore(test_config):
            pass
        monkeypatch.setattr(hooks, "_load_llm_config", _llm_config)
        monkeypatch.setattr(
            hooks,
            "_extract_candidates",
            lambda *_: [
                CandidateMemory(
                    key="project:proj:playbook:stdio-triage",
                    value="排查握手卡死先确认症状，再定位读取路径，最后回归验证。",
                    attribute="playbook",
                    confidence=0.8,
                    importance=7.0,
                ),
                CandidateMemory(
                    key="SESSION_SUMMARY",
                    value="本次会话验证了 playbook 的候选隔离。",
                    confidence=0.9,
                    tags=["日志"],
                ),
            ],
        )

        result = hooks.session_end({"session_id": "session_playbook_isolation"})

        assert result.status == "completed"
        assert result.persisted == 2  # summary + 隔离的 playbook
        items = self._rows(test_config, "context_items")
        by_key = {item["identity_key"]: item for item in items}
        isolated = by_key["project:proj:playbook:stdio-triage"]
        assert isolated["status"] == "candidate"
        assert isolated["content_type"] == "playbook"
        assert isolated["source_count"] == 1
        # 候选隔离：无 legacy 投影行，memories 表只有 summary
        memories = self._rows(test_config, "memories")
        assert len(memories) == 1
        assert ":progress:log:" in memories[0]["key"]


class TestSessionEndConsolidationWiring:
    """P4b：persist 成功后用既有 LLM chat 能力跑一次 consolidation。"""

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

    @staticmethod
    def _persisted_candidates():
        return [
            CandidateMemory(
                key="project:proj:fact:consolidation",
                value="consolidation 接线后跑一次晋升与 playbook 评估。",
                attribute="fact",
                confidence=0.9,
                importance=6.0,
            ),
            CandidateMemory(
                key="SESSION_SUMMARY",
                value="本次会话验证了 consolidation 的 LLM 接线。",
                confidence=0.9,
                tags=["日志"],
            ),
        ]

    def _prepare(self, monkeypatch, tmp_path, test_config, mode):
        test_config.context_mode = mode
        self._wire_session(monkeypatch, tmp_path, test_config)
        with MemoryStore(test_config):
            pass
        monkeypatch.setattr(hooks, "_load_llm_config", _llm_config)
        monkeypatch.setattr(
            hooks, "_extract_candidates", lambda *_: self._persisted_candidates(),
        )

    def test_shadow_mode_runs_consolidation_with_llm_callable(
            self, monkeypatch, tmp_path, test_config):
        from evolvmem.context_service import ContextService

        self._prepare(monkeypatch, tmp_path, test_config, "shadow")
        captured = {}

        def spy(service, *, llm=None, embedding_engine=None):
            captured["llm"] = llm
            captured["embedding_engine"] = embedding_engine

        monkeypatch.setattr(ContextService, "run_consolidation", spy)
        chats = []

        def fake_chat(prompt, config, *args, **kwargs):
            chats.append(prompt)
            return "ok"

        monkeypatch.setattr(hooks, "_call_llm_with_retry", fake_chat)

        result = hooks.session_end({"session_id": "session_consolidation"})

        assert result.status == "completed"
        assert result.persisted == 2
        assert callable(captured["llm"])
        # llm callable 包装既有 chat 能力：成功返回文本，异常降级为 None
        assert captured["llm"]("提炼 playbook") == "ok"
        assert chats[-1] == "提炼 playbook"
        assert any("项目知识整理助手" in prompt for prompt in chats[:-1])

        def failing_chat(*args, **kwargs):
            raise hooks.RetryableExtractionError("provider down")

        monkeypatch.setattr(hooks, "_call_llm_with_retry", failing_chat)
        assert captured["llm"]("再试一次") is None

    def test_session_end_passes_llm_callable_to_persistence(
            self, monkeypatch, tmp_path, test_config):
        from evolvmem.context_service import ContextService

        self._prepare(monkeypatch, tmp_path, test_config, "shadow")
        original = ContextService.persist_legacy_extraction
        captured = {}

        def spy(service, request, **kwargs):
            captured["llm"] = kwargs.get("llm")
            return original(service, request, **kwargs)

        monkeypatch.setattr(ContextService, "persist_legacy_extraction", spy)
        monkeypatch.setattr(ContextService, "run_consolidation", lambda *args, **kwargs: None)
        monkeypatch.setattr(
            hooks, "_call_llm_with_retry", lambda prompt, config: "滚动摘要模型结果"
        )

        result = hooks.session_end({"session_id": "session_rollup_wiring"})

        assert result.status == "completed"
        assert callable(captured["llm"])
        assert captured["llm"]("更新项目摘要") == "滚动摘要模型结果"

    def test_consolidation_failure_does_not_change_result(
            self, monkeypatch, tmp_path, test_config):
        from evolvmem.context_service import ContextService

        self._prepare(monkeypatch, tmp_path, test_config, "shadow")

        def boom(service, **kwargs):
            raise RuntimeError("synthetic consolidation failure at /secret/path")

        monkeypatch.setattr(ContextService, "run_consolidation", boom)
        logs = []
        monkeypatch.setattr(hooks, "_log", logs.append)

        result = hooks.session_end({"session_id": "session_consolidation_fail"})

        assert result.status == "completed"
        assert result.persisted == 2
        assert any("consolidation" in line for line in logs)
        assert "/secret/path" not in "\n".join(logs)

    @pytest.mark.parametrize("mode", ["legacy", "compat"])
    def test_legacy_and_compat_skip_consolidation(
            self, monkeypatch, tmp_path, test_config, mode):
        from evolvmem.context_service import ContextService

        self._prepare(monkeypatch, tmp_path, test_config, mode)
        calls = []
        monkeypatch.setattr(
            ContextService,
            "run_consolidation",
            lambda service, **kwargs: calls.append(1),
        )

        result = hooks.session_end({"session_id": f"session_{mode}_skip"})

        assert result.status == "completed"
        assert calls == []
