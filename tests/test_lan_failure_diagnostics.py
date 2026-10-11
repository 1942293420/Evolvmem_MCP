"""Unexpected failures remain private but their categories stay diagnosable."""
import logging
import json
from tests.test_lan_sharing import lan


def test_failure_category_is_logged_without_body_or_exception_message(lan, monkeypatch, caplog):
    _, adapter = lan
    def broken(*args, **kwargs):
        raise RuntimeError('PRIVATE_DIALOGUE_AND_TOKEN')
    monkeypatch.setattr(adapter, '_call_tool', broken)
    with caplog.at_level(logging.WARNING, logger='evolvmem.lan_tools'):
        result = adapter.call_tool('jiangli', 'knowledge_recall', {'query':'PRIVATE_QUERY'})
    assert result == {'error':'remote_tool_failed'}
    assert 'RuntimeError' in caplog.text and 'knowledge_recall' in caplog.text
    assert 'PRIVATE_' not in caplog.text


def test_failure_has_correlated_stage_and_safe_code_location(lan, monkeypatch, caplog):
    from evolvmem.request_trace import stage
    _, adapter = lan
    def broken(*args, **kwargs):
        with stage('rerank'):
            raise RuntimeError('PRIVATE_DIALOGUE_AND_TOKEN')
    monkeypatch.setattr(adapter, '_call_tool', broken)
    with caplog.at_level(logging.WARNING):
        result = adapter.call_tool('jiangli', 'knowledge_recall', {'query': 'PRIVATE_QUERY'})
    assert result['error'] == 'remote_tool_failed'
    report = next(json.loads(r.message.split('request_trace ', 1)[1]) for r in caplog.records if 'request_trace ' in r.message)
    assert len(report['trace_id']) == 32
    assert report['tool'] == 'knowledge_recall'
    assert report['error']['category'] == 'RuntimeError'
    assert report['error']['stage'] == 'rerank'
    assert report['stages_ms']['rerank'] >= 0
    assert report['stages_ms']['queue'] >= 0
    assert report['error']['frames'], 'retain our dispatch code position, not exception text or locals'
    assert 'PRIVATE_' not in caplog.text


def test_http_and_direct_calls_log_once_and_requests_do_not_share_trace(lan, monkeypatch, caplog):
    import evolvmem.request_trace as tracing
    _, adapter = lan
    monkeypatch.setattr(tracing, 'SLOW_MS', 0)
    with caplog.at_level(logging.WARNING):
        for ident in (1, 2):
            response = adapter.handle_request('jiangli', {'jsonrpc': '2.0', 'id': ident,
                'method': 'tools/call', 'params': {'name': 'context_status', 'arguments': {}}})
            assert 'result' in response
    reports = [json.loads(r.message.split('request_trace ', 1)[1]) for r in caplog.records if 'request_trace ' in r.message]
    assert len(reports) == 2
    assert reports[0]['trace_id'] != reports[1]['trace_id']
    assert all(r['tool'] == 'context_status' and r['stages_ms']['queue'] >= 0 for r in reports)
