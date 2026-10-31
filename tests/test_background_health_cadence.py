"""Bound repeated full scans and expose private-worker timing safely."""
import json
import logging
from types import SimpleNamespace

from tests.test_lan_sharing import lan


def test_default_coalesces_changes_for_thirty_seconds(lan, monkeypatch):
    from evolvmem import background_health
    runtime, _ = lan
    prior = runtime._background_health
    prior.close()
    clock = [background_health.time.monotonic()]
    monkeypatch.setattr(background_health, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    manager = type(prior)(prior.servers, start=False)
    try:
        manager.refresh('kane')
        assert manager.run_once()
        clock[0] += 10
        for _ in range(20):
            manager.refresh('kane')
            assert manager.status('kane')['refresh_pending']
            assert manager.run_once() is False
        clock[0] += 21
        assert manager.run_once()
    finally:
        manager.close()


def test_worker_exception_has_content_free_trace(lan, monkeypatch, caplog):
    runtime, _ = lan
    prior = runtime._background_health
    prior.close()
    manager = type(prior)(prior.servers, start=False)
    def failed(_):
        raise RuntimeError('PRIVATE BODY /private/token')
    monkeypatch.setattr(manager, '_evaluate', failed)
    try:
        caplog.set_level(logging.WARNING)
        manager.refresh('kane')
        assert manager.run_once()
        traces = [json.loads(record.message.split('request_trace ', 1)[1])
                  for record in caplog.records if 'request_trace ' in record.message]
        assert len(traces) == 1
        assert traces[0]['tool'] == 'background_health'
        assert traces[0]['error']['category'] == 'RuntimeError'
        assert 'PRIVATE BODY' not in caplog.text and '/private/token' not in caplog.text
        assert manager.status('kane')['hard_gate'] != 'allowed'
    finally:
        manager.close()


def test_physical_check_does_not_spend_the_semantic_sql_budget(lan, monkeypatch):
    from evolvmem import background_health
    runtime, _ = lan
    prior = runtime._background_health
    prior.close()
    clock = [background_health.time.monotonic()]
    monkeypatch.setattr(background_health, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    manager = type(prior)(prior.servers, start=False)
    def physical(_name, _path, _expected):
        clock[0] += 125  # Simulate observed slow physical scan; no real waiting.
        return (), clock[0]
    monkeypatch.setattr(manager, '_physical_result', physical)
    try:
        manager.refresh('kane')
        assert manager.run_once()
        status = manager.status('kane')
        assert status['hard_gate'] == 'allowed', status
        assert status['error'] is None
        assert status['diagnostic']['last_checked_ready'] is True
    finally:
        manager.close()


def test_semantic_sql_budget_still_expires_after_the_physical_check(lan, monkeypatch):
    from evolvmem import background_health
    from evolvmem.context_service import ContextService
    runtime, _ = lan
    prior = runtime._background_health
    prior.close()
    clock = [background_health.time.monotonic()]
    monkeypatch.setattr(background_health, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    manager = type(prior)(prior.servers, start=False)
    original = ContextService.evaluated_status
    def expired(service):
        clock[0] += 61
        return original(service)
    monkeypatch.setattr(ContextService, 'evaluated_status', expired)
    try:
        manager.refresh('kane')
        assert manager.run_once()
        status = manager.status('kane')
        assert status['hard_gate'] != 'allowed'
        assert status['error'] == 'health_evaluation_failed'
        assert status['diagnostic'] is None
    finally:
        manager.close()


def test_initial_observation_keeps_its_existing_sql_budget(lan, monkeypatch):
    from evolvmem import background_health
    runtime, _ = lan
    prior = runtime._background_health
    prior.close()
    clock = [background_health.time.monotonic()]
    monkeypatch.setattr(background_health, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    manager = type(prior)(prior.servers, start=False)
    original = manager._observation
    initial = [True]
    def expired(name, conn):
        if initial[0]:
            initial[0] = False
            clock[0] += 61
            # Real SQLite work reaches the existing progress interval.
            conn.execute('WITH RECURSIVE ticks(n) AS (SELECT 1 UNION ALL '
                         'SELECT n+1 FROM ticks WHERE n<1000) SELECT sum(n) FROM ticks').fetchone()
        return original(name, conn)
    monkeypatch.setattr(manager, '_observation', expired)
    try:
        manager.refresh('kane')
        assert manager.run_once()
        monkeypatch.setattr(manager, '_observation', original)
        status = manager.status('kane')
        assert status['hard_gate'] != 'allowed'
        assert status['error'] == 'health_evaluation_failed'
        assert status['diagnostic'] is None
    finally:
        manager.close()
