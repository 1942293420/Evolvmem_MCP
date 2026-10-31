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
