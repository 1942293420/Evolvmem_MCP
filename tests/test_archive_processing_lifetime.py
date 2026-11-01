"""Transport copies must not survive across extraction provider calls."""
import json
import weakref

from tests.test_lan_sharing import lan
from tests.test_lan_capture import transcript, upload


class TransportText(str):
    pass


class ParsedEnvelope(dict):
    pass


class ParsedRows(list):
    pass


def test_transport_released_before_provider_without_losing_archive(lan, monkeypatch):
    from evolvmem import kimi_hooks, codex_transcript, session_extraction
    runtime, adapter = lan
    raw = transcript(text='完整原文保留；这里只释放提取阶段用完的内存副本。')
    saved = upload(adapter, raw, extract=True)
    assert saved['extraction_status'] == 'pending'
    capture = adapter.captures['jiangli']
    original_read = capture.archiver.read_payload
    original_loads = json.loads
    original_parse = codex_transcript.parse_transcript
    refs = {}
    expected_messages = original_parse(raw, 'session-1')[1]
    observed = []

    def read(archive_id):
        value = TransportText(original_read(archive_id))
        refs['payload'] = weakref.ref(value)
        return value

    def loads(value, *args, **kwargs):
        result = original_loads(value, *args, **kwargs)
        if isinstance(value, TransportText):
            result = ParsedEnvelope(result)
            refs['envelope'] = weakref.ref(result)
        return result

    def parse(value, session_id):
        rows, messages = original_parse(value, session_id)
        rows = ParsedRows(rows)
        refs['rows'] = weakref.ref(rows)
        return rows, messages

    def prepare(config, project, source, messages, credentials, **options):
        observed.append((messages == expected_messages,
                         {name: ref() is None for name, ref in refs.items()}))
        # A provider failure must still keep the encrypted original retryable.
        raise RuntimeError('fixture_provider_failure')

    monkeypatch.setattr(capture.archiver, 'read_payload', read)
    monkeypatch.setattr(json, 'loads', loads)
    monkeypatch.setattr(codex_transcript, 'parse_transcript', parse)
    monkeypatch.setattr(kimi_hooks, '_load_llm_config', lambda **kwargs: object())
    monkeypatch.setattr(session_extraction, 'prepare_extraction', prepare)
    assert adapter.process_pending() == 1
    assert observed == [(True, {'payload': True, 'envelope': True, 'rows': True})]
    original = original_loads(original_read(saved['archive_id']))
    assert original['transcript'].encode('utf-8') == raw
    status = adapter.call_tool('jiangli', 'session_archive_status', {
        'device_id': 'windows-main', 'session_id': 'session-1'})
    assert status['status'] == 'archived'
    assert status['extraction_status'] == 'failed'
    assert status['processing_error'] == 'extraction_failed:RuntimeError'


def test_failed_provider_can_retry_same_complete_archive_and_persist(lan, monkeypatch):
    from evolvmem import kimi_hooks
    runtime, adapter = lan
    raw = transcript(text='重试必须沿用同一个完整原文归档，并正常写入提取摘要。')
    saved = upload(adapter, raw, extract=True)
    credentials = runtime.settings.owner_data_dir / 'llm_credentials.json'
    credentials.write_text(json.dumps({'provider': 'deepseek', 'api_key': 'fixture-only'}))
    fail = True

    def provider(prompt, *args, **kwargs):
        if fail:
            raise RuntimeError('fixture_provider_failure')
        return json.dumps({'memories': [{'key': 'SESSION_SUMMARY',
            'value': '本次确认重试继续使用同一个完整会话原文归档，并核验提取摘要正常保存。'}]},
            ensure_ascii=False)

    monkeypatch.setattr(kimi_hooks, '_call_llm_with_retry', provider)
    assert adapter.process_pending() == 1
    identity = {'device_id': 'windows-main', 'session_id': 'session-1'}
    status = adapter.call_tool('jiangli', 'session_archive_status', identity)
    assert status['extraction_status'] == 'failed'
    assert status['archive_id'] == saved['archive_id']
    retry = adapter.call_tool('jiangli', 'session_archive_retry', {
        **identity, 'request_id': 'same-archive-lifetime-retry'})
    assert retry['extraction_status'] == 'pending'
    assert retry['archive_id'] == saved['archive_id']
    fail = False
    assert adapter.process_pending() == 1
    status = adapter.call_tool('jiangli', 'session_archive_status', identity)
    assert status['extraction_status'] == 'extracted', status
    assert status['archive_id'] == saved['archive_id']
    assert status['extraction']['summary_context_id'] > 0
    capture = adapter.captures['jiangli']
    assert json.loads(capture.archiver.read_payload(saved['archive_id']))['transcript'].encode('utf-8') == raw
