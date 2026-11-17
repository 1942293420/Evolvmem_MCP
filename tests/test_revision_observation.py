"""Revision failure phases are precise while existing wire/SQLite semantics stay."""
import json
import sqlite3
from types import SimpleNamespace

import pytest

from evolvmem import lan_revision, request_trace as tracing
from evolvmem.revision_observation import RevisionObservation, classified


@pytest.fixture
def reader(tmp_path):
    settings = SimpleNamespace(owner_data_dir=tmp_path / 'owner', data_dir=tmp_path / 'lan')
    paths = (settings.owner_data_dir / 'memory.db', settings.data_dir / 'users' / 'kane' / 'memory.db',
             settings.data_dir / 'public' / 'memory.db')
    for path in paths:
        path.parent.mkdir(parents=True)
        conn = sqlite3.connect(path)
        try:
            conn.execute('CREATE TABLE lan_memory_revision(id INTEGER PRIMARY KEY, revision INTEGER)')
            conn.execute('INSERT INTO lan_memory_revision VALUES(1,0)')
            conn.commit()
        finally:
            conn.close()
    return lan_revision.RevisionReader(settings), paths[-1]


def test_revision_attach_busy_is_classified_without_private_details(reader, caplog):
    revision, public = reader
    blocker = sqlite3.connect(public)
    blocker.execute('BEGIN EXCLUSIVE')
    try:
        with caplog.at_level('WARNING'):
            with pytest.raises(sqlite3.OperationalError) as raised:
                with tracing.request_trace('memory_status'):
                    with tracing.stage('dispatch'):
                        revision.read('kane')
    finally:
        blocker.rollback()
        blocker.close()
    assert raised.value.sqlite_errorcode == sqlite3.SQLITE_BUSY
    report, = [json.loads(r.message.split('request_trace ', 1)[1]) for r in caplog.records
               if r.message.startswith('request_trace ')]
    observed = report['revision']
    assert observed['phase'] == 'attach_public'
    assert observed['failure']['classification'] == 'sqlite_busy'
    assert observed['failure']['sqlite_primary_code'] == sqlite3.SQLITE_BUSY
    assert observed['deadline_requested'] is False
    assert report['error']['category'] == 'OperationalError'
    assert report['error']['stage'] == 'dispatch'
    assert 'queue' not in report['stages_ms']
    for value in (str(public), 'ATTACH DATABASE', 'file:', 'Bearer', 'kane'):
        assert value not in caplog.text
    assert revision.read('kane') == '0.0'


def test_actual_progress_interrupt_has_explicit_deadline_evidence():
    clock = [0.0]
    observed = RevisionObservation(clock=lambda: clock[0])
    observed.arm(.25)
    clock[0] = .3
    conn = sqlite3.connect(':memory:')
    conn.set_progress_handler(observed.progress, 1)
    try:
        with pytest.raises(sqlite3.OperationalError) as raised:
            with observed.phase('attach_public'):
                conn.execute("ATTACH DATABASE ':memory:' AS public")
    finally:
        conn.close()
    assert raised.value.sqlite_errorcode == sqlite3.SQLITE_INTERRUPT
    assert observed.snapshot()['failure']['classification'] == 'revision_deadline'
    assert observed.snapshot()['deadline_requested'] is True
    assert classified(raised.value, False)['classification'] == 'sqlite_interrupted'


def test_extended_codes_and_unknown_phase_cannot_leak_exception_strings():
    observed = RevisionObservation()
    exc = sqlite3.OperationalError('PRIVATE_QUERY Bearer PRIVATE_TOKEN')
    exc.sqlite_errorcode = sqlite3.SQLITE_BUSY | (2 << 8)
    exc.sqlite_errorname = 'PRIVATE_NAME'
    with pytest.raises(sqlite3.OperationalError) as raised:
        with observed.phase('PRIVATE_PHASE'):
            raise exc
    assert raised.value is exc
    record = observed.snapshot()
    assert record['phase'] == 'unknown'
    assert record['failure']['sqlite_code'] == 517
    assert record['failure']['classification'] == 'sqlite_busy'
    assert 'PRIVATE_' not in json.dumps(record)


def test_failing_trace_sink_cannot_replace_success_or_original_error(reader, monkeypatch):
    revision, public = reader
    def unavailable(_):
        raise RuntimeError('PRIVATE_TELEMETRY_ERROR')
    monkeypatch.setattr(lan_revision, 'revision_observation', unavailable)
    assert revision.read('kane') == '0.0'
    conn = sqlite3.connect(public)
    try:
        conn.execute('DROP TABLE lan_memory_revision')
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(sqlite3.OperationalError) as raised:
        revision.read('kane')
    assert raised.value.sqlite_errorcode == sqlite3.SQLITE_ERROR
