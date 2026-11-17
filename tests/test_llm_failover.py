"""Provider failover retains a bounded deadline and isolates credentials."""
import io
import json
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest

from evolvmem import kimi_hooks as hooks


def pair():
    backup = hooks.LLMConfig('mimo', 'backup-test-key', 'https://backup.test/v1/chat/completions', 'mimo-v2.6-flash')
    return hooks.LLMConfig('deepseek', 'primary-test-key', 'https://primary.test/chat/completions', 'deepseek-v4-flash', fallback=backup)


@pytest.fixture(autouse=True)
def clean_cooldown(monkeypatch):
    monkeypatch.setattr(hooks, '_PROVIDER_RETRY_AFTER', {}, raising=False)
    monkeypatch.setattr(hooks, '_log', lambda message: None)


def test_load_primary_and_independent_backup_credentials(tmp_path):
    path = tmp_path / 'llm_credentials.json'
    path.write_text(json.dumps({'provider': 'deepseek', 'api_key': 'primary-test-key',
                               'fallback': {'provider': 'mimo', 'api_key': 'backup-test-key'}}))
    config = hooks._load_llm_config(config_path=path)
    assert config.api_key == 'primary-test-key'
    assert config.fallback.api_key == 'backup-test-key'
    assert config.fallback.model == 'mimo-v2.6-flash'
    assert config.fallback.base_url == 'https://token-plan-cn.xiaomimimo.com/v1/chat/completions'
    assert 'test-key' not in repr(config)


def test_invalid_backup_does_not_disable_primary(tmp_path):
    path = tmp_path / 'llm_credentials.json'
    path.write_text(json.dumps({'api_key': 'primary-test-key', 'fallback': {'provider': 'unknown'}}))
    config = hooks._load_llm_config(config_path=path)
    assert config.api_key == 'primary-test-key' and config.fallback is None


def test_healthy_primary_never_calls_backup(monkeypatch):
    calls = []
    monkeypatch.setattr(hooks, '_call_llm', lambda prompt, config, **kw: calls.append(config.provider) or 'primary result')
    assert hooks._call_llm_with_retry('prompt', pair()) == 'primary result'
    assert calls == ['deepseek']


@pytest.mark.parametrize('failure', [TimeoutError(), HTTPError('https://primary.test', 503, '', {}, io.BytesIO(b'{}')),
                                     HTTPError('https://primary.test', 401, '', {}, io.BytesIO(b'{}')),
                                     HTTPError('https://primary.test', 429, '', {}, io.BytesIO(b'{}'))])
def test_failure_switches_once_without_primary_backoff(monkeypatch, failure):
    calls = []
    def call(prompt, config, **kw):
        calls.append((config.provider, prompt))
        if config.provider == 'deepseek':
            raise failure
        return 'backup result'
    monkeypatch.setattr(hooks, '_call_llm', call)
    monkeypatch.setattr(hooks.time, 'sleep', lambda seconds: pytest.fail('primary must not back off before failover'))
    assert hooks._call_llm_with_retry('same input', pair()) == 'backup result'
    assert calls == [('deepseek', 'same input'), ('mimo', 'same input')]


def test_timeout_reserves_budget_and_cooldown_recovers_primary(monkeypatch):
    clock, calls = [100.0], []
    def call(prompt, config, *, deadline):
        calls.append((config.provider, deadline))
        if config.provider == 'deepseek' and len(calls) == 1:
            clock[0] = deadline
            raise TimeoutError()
        return config.provider
    monkeypatch.setattr(hooks.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(hooks, '_call_llm', call)
    config = pair()
    assert hooks._call_llm_with_retry('prompt', config, deadline=190) == 'mimo'
    assert calls == [('deepseek', 125), ('mimo', 190)]
    assert hooks._call_llm_with_retry('next chunk', config, deadline=190) == 'mimo'
    clock[0] = 186
    assert hooks._call_llm_with_retry('later', config, deadline=276) == 'deepseek'


def test_small_budget_is_shared_and_expired_budget_never_calls(monkeypatch):
    clock, calls = [10.0], []
    def call(prompt, config, *, deadline):
        calls.append((config.provider, deadline))
        clock[0] = deadline
        raise TimeoutError()
    monkeypatch.setattr(hooks.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(hooks, '_call_llm', call)
    with pytest.raises(hooks.RetryableExtractionError):
        hooks._call_llm_with_retry('prompt', pair(), deadline=16)
    assert calls == [('deepseek', 12), ('mimo', 16)]
    with pytest.raises(hooks.RetryableExtractionError):
        hooks._call_llm_with_retry('prompt', pair(), deadline=16)
    assert len(calls) == 2


def test_context_overflow_preserves_chunking_instead_of_switching(monkeypatch):
    calls = []
    def call(prompt, config, **kw):
        calls.append(config.provider)
        raise HTTPError(config.base_url, 400, '', {}, io.BytesIO(b'{"error":"context_length_exceeded"}'))
    monkeypatch.setattr(hooks, '_call_llm', call)
    with pytest.raises(hooks.ContextOverflowError):
        hooks._call_llm_with_retry('too large', pair())
    assert calls == ['deepseek']


def test_backup_auth_failure_remains_actionable(monkeypatch):
    def call(prompt, config, **kw):
        raise HTTPError(config.base_url, 401, '', {}, io.BytesIO(b'{}'))
    monkeypatch.setattr(hooks, '_call_llm', call)
    with pytest.raises(hooks.RetryableExtractionError) as error:
        hooks._call_llm_with_retry('prompt', pair())
    assert error.value.halt_run is True
    assert 'mimo HTTP 401' in str(error.value)
    assert 'test-key' not in str(error.value)


def test_mimo_sends_own_key_and_json_non_thinking_parameters(monkeypatch):
    captured = {}
    class Opener:
        def open(self, request, timeout):
            captured.update(body=json.loads(request.data), headers=dict(request.header_items()))
            return io.BytesIO(b'{"choices":[{"message":{"content":"{\\"ok\\":true}"}}]}')
    monkeypatch.setattr(hooks.urllib.request, 'build_opener', lambda *args: Opener())
    assert json.loads(hooks._call_llm('return JSON', pair().fallback)) == {'ok': True}
    assert captured['headers']['Api-key'] == 'backup-test-key'
    assert 'Authorization' not in captured['headers']
    assert captured['body']['max_completion_tokens'] == 4096
    assert 'max_tokens' not in captured['body']
    assert captured['body']['thinking'] == {'type': 'disabled'}
    assert captured['body']['response_format'] == {'type': 'json_object'}


def test_provider_progress_is_scoped_and_marks_backup(monkeypatch):
    events = []
    def call(prompt, config, **kw):
        if config.provider == 'deepseek': raise TimeoutError()
        return 'ok'
    monkeypatch.setattr(hooks, '_call_llm', call)
    with hooks.llm_provider_status(events.append):
        assert hooks._call_llm_with_retry('prompt', pair()) == 'ok'
    assert [(e['provider'], e['fallback'], e['state']) for e in events] == [
        ('deepseek', False, 'running'), ('deepseek', False, 'failed'),
        ('mimo', True, 'running'), ('mimo', True, 'succeeded')]
    hooks._call_llm_with_retry('next', pair())
    assert len(events) == 4


def test_response_body_uses_time_remaining_after_headers(monkeypatch):
    clock, timeouts = [0.0], []
    class Response(io.BytesIO):
        fp = SimpleNamespace(raw=SimpleNamespace(_sock=SimpleNamespace(settimeout=timeouts.append)))
        def read1(self, count):
            clock[0] += 1
            return super().read1(count)
    class Opener:
        def open(self, request, timeout):
            clock[0] = 8.0
            return Response(b'{"choices":[{"message":{"content":"ok"}}]}')
    monkeypatch.setattr(hooks.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(hooks.urllib.request, 'build_opener', lambda *args: Opener())
    with pytest.raises(hooks.RetryableExtractionError, match='budget exhausted'):
        hooks._call_llm('prompt', pair().fallback, deadline=10)
    assert timeouts == [2, 1]


@pytest.mark.parametrize('raw', [b'not json', b'{"choices":[]}', b'{"choices":[{"message":{"content":null}}]}'])
def test_invalid_provider_envelope_uses_backup(monkeypatch, raw):
    calls = []
    class Opener:
        def open(self, request, timeout):
            calls.append(request.full_url)
            return io.BytesIO(raw if len(calls) == 1 else b'{"choices":[{"message":{"content":"ok"}}]}')
    monkeypatch.setattr(hooks.urllib.request, 'build_opener', lambda *args: Opener())
    assert hooks._call_llm_with_retry('prompt', pair()) == 'ok'
    assert calls == [pair().base_url, pair().fallback.base_url]
