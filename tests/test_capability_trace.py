"""Content-free stage timings preserve capability retrieval and fallback behavior."""
import json
import logging

import pytest

from evolvmem import request_trace as tracing
from evolvmem.capability_index import CapabilityIndex
from evolvmem.capability_recall_eligibility import eligible_ids
from evolvmem.capability_retriever import CapabilityRetriever
from evolvmem.context_models import ContextMode
from evolvmem.context_service import ContextService
from tests.test_capability_index import FakeEngine
from tests.test_capability_retriever import add_capability


def reports(caplog):
    return [json.loads(row.message.split('request_trace ', 1)[1])
            for row in caplog.records if 'request_trace ' in row.message]


@pytest.fixture
def traced_service(test_config):
    test_config.embedding_dim = 2
    service = ContextService(test_config)
    service.initialize(mode=ContextMode.SHADOW, adapter='codex')
    try:
        ident = add_capability(service, title='PRIVATE_QUERY_TRACE',
                               content='PRIVATE_BODY_TRACE 提交前校验。')
        index = CapabilityIndex(service.store, test_config, FakeEngine(),
                                eligible_ids=lambda ids: eligible_ids(service.store, ids))
        assert index.sync(ident, 'ignored caller text') == 'synchronized'
        yield service, index, ident
    finally:
        service.close()


def test_search_and_selected_proofs_report_distinct_nested_stages_without_content(
        traced_service, monkeypatch, caplog):
    service, index, ident = traced_service
    monkeypatch.setattr(service, 'capability_index', lambda: index)
    retriever = CapabilityRetriever(service)
    expected = retriever.validate_current_results(
        retriever.search('PRIVATE_QUERY_TRACE', project='bluewhale'), project='bluewhale')
    assert [row['memory_id'] for row in expected] == [ident]
    monkeypatch.setattr(tracing, 'SLOW_MS', 0)
    with caplog.at_level(logging.WARNING):
        with tracing.request_trace('capability_search'):
            result = retriever.validate_current_results(
                retriever.search('PRIVATE_QUERY_TRACE', project='bluewhale'), project='bluewhale')
            with tracing.stage('PRIVATE_STAGE_TRACE'):
                pass
    assert result == expected
    report, = reports(caplog)
    durations = report['stages_ms']
    required = {
        'capability_search', 'capability_vector', 'capability_embedding',
        'capability_lock_wait', 'capability_lock_held', 'capability_vector_eligibility',
        'capability_lexical', 'capability_facts', 'capability_modules',
        'capability_selected_validation', 'capability_selected_facts', 'capability_selected_modules',
    }
    assert required <= durations.keys()
    assert all(value >= 0 for value in durations.values())
    for parent, children in (
        ('capability_search', ('capability_vector', 'capability_lexical', 'capability_facts', 'capability_modules')),
        ('capability_vector', ('capability_embedding', 'capability_lock_wait', 'capability_lock_held',
                               'capability_vector_eligibility')),
        ('capability_selected_validation', ('capability_selected_facts', 'capability_selected_modules')),
    ):
        assert sum(durations[child] for child in children) <= durations[parent] + .01
    assert report['error'] is None
    assert 'PRIVATE_' not in caplog.text
    assert str(service.config.data_dir) not in caplog.text


def test_failed_vector_eligibility_remains_fallback_and_releases_lock(
        traced_service, monkeypatch, caplog):
    service, index, _ = traced_service
    def unavailable(_):
        raise RuntimeError('PRIVATE_ERROR_TRACE')
    index.eligible_ids = unavailable
    monkeypatch.setattr(tracing, 'SLOW_MS', 0)
    with caplog.at_level(logging.WARNING):
        with tracing.request_trace('capability_search'):
            assert index.search('PRIVATE_QUERY_TRACE') == []
    report, = reports(caplog)
    assert report['error']['category'] == 'RuntimeError'
    assert report['error']['stage'] == 'capability_vector_eligibility'
    assert {'capability_lock_wait', 'capability_lock_held', 'capability_vector_eligibility'} <= report['stages_ms'].keys()
    assert 'PRIVATE_' not in caplog.text
    assert str(service.config.data_dir) not in caplog.text
    if index._supports_shared_recovery():
        import fcntl
        with index.vector_index.path.with_suffix('.recovery.lock').open('a') as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
