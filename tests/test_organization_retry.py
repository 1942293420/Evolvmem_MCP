"""Bounded recovery of real per-unit failures, using synthetic archives only."""
import json

import pytest

from evolvmem.kimi_hooks import RetryableExtractionError
from evolvmem.lan_sharing import LanError
from tests.test_auto_organization_extraction import service, org, _task_for
from tests.test_history_qa_memory import service as base_service
from tests.unit_model_fixture import UnitProvider, many_topic_archive, run_worker


class FailingOnceProvider(UnitProvider):
    def __init__(self, error, *, always=False):
        super().__init__()
        self.error = error
        self.always = always
        self.attempts = 0

    def extract(self, prompt):
        self.attempts += 1
        if self.always or self.attempts == 1:
            raise self.error
        return super().extract(prompt)


def enqueue(service, session):
    source = many_topic_archive(service, session=session,
                                text='用户要求：Evo 演示项目先明确验收条件。')
    return _task_for(service, source)


@pytest.mark.parametrize('error', [TimeoutError('synthetic private error'),
                                  LanError('extraction_summary_missing')])
def test_transient_unit_failure_retries_once_and_finishes_without_duplicate_history(service, monkeypatch, error):
    task_id = enqueue(service, 'transient-unit')
    provider = FailingOnceProvider(error)
    run_worker(service, monkeypatch, provider)

    task = org(service, '/detail', {'task_id': task_id})
    assert task['status'] == 'completed'
    assert task['attempts'] == 3, 'one retry after segmentation and the initial extraction'
    assert provider.attempts == 2
    units = org(service, '/units', {'task_id': task_id})['items']
    assert len(units) == 1 and units[0]['extraction_stage'] == 'done'
    assert service.store._connection().execute(
        "SELECT COUNT(*) FROM unit_derivations WHERE unit_task_id=? AND kind='history'",
        (task_id,)).fetchone()[0] == 1


def test_repeated_transient_failure_exhausts_budget_and_remains_visible(service, monkeypatch):
    task_id = enqueue(service, 'repeated-timeout')
    provider = FailingOnceProvider(TimeoutError('synthetic private error'), always=True)
    run_worker(service, monkeypatch, provider)

    task = org(service, '/detail', {'task_id': task_id})
    assert task['status'] == 'failed' and task['attempts'] == 3
    assert provider.attempts == 2
    unit = org(service, '/units', {'task_id': task_id})['items'][0]
    diagnostic = json.loads(unit['extraction_diagnostic'])
    assert diagnostic['retryable'] is True
    assert 'synthetic private error' not in unit['extraction_diagnostic']


@pytest.mark.parametrize('error', [ValueError('private invalid payload'),
                                  RetryableExtractionError('private credentials', halt_run=True)])
def test_permanent_failure_waits_for_explicit_retry_and_can_then_recover(service, monkeypatch, error):
    task_id = enqueue(service, 'manual-retry')
    provider = FailingOnceProvider(error)
    run_worker(service, monkeypatch, provider)

    task = org(service, '/detail', {'task_id': task_id})
    assert task['status'] == 'failed' and task['attempts'] == 2
    assert provider.attempts == 1
    unit = org(service, '/units', {'task_id': task_id})['items'][0]
    diagnostic = json.loads(unit['extraction_diagnostic'])
    assert diagnostic['retryable'] is False
    assert 'private' not in unit['extraction_diagnostic']

    org(service, '/retry', {'task_id': task_id})
    run_worker(service, monkeypatch, provider)

    assert org(service, '/detail', {'task_id': task_id})['status'] == 'completed'
    assert provider.attempts == 2
