"""Unexpected failures remain private but their categories stay diagnosable."""
import logging
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
