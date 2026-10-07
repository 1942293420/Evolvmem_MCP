"""Incremental local Codex batches are independent sources inside one session.

The real ``LocalCodexCapture`` writes several immutable batches for one session,
each with its own source range and content digest. The Windows client-reported
path instead re-uploads whole snapshots of one session, where only the newest is
current. Splitting every Codex identity at ':' collapses the first case into the
second: a newer batch hides an older one that is still queued.

All data here is synthetic and lives under pytest's temporary directory; the real
Codex tree, the real database, credentials and models are never touched.
"""
import hashlib
import json
import os
import time
from datetime import datetime, timezone

import pytest

from evolvmem.config import Config
from evolvmem.context_models import ContextMode
from evolvmem.knowledge_api import dispatch
from evolvmem.local_codex_capture import LocalCodexCapture
from evolvmem.session_archive import SessionArchiver
from evolvmem.session_identity import is_incremental_batch, logical_identity
from tests.test_web_server import _make_service
from tests.unit_model_fixture import model_for, run_worker

SESSION = '01a0fb28-893d-7250-9445-1a2c2fe6a0ab'
NEW = datetime(2026, 6, 1, 0, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def config(tmp_path):
    data = tmp_path / 'data'
    data.mkdir()
    return Config(data_dir=data, apply_environment=False)


@pytest.fixture
def service(config):
    instance = _make_service(config, mode=ContextMode.SHADOW)
    for project in ('evo', 'shop'):
        instance.knowledge().save_project({'project': project})
    yield instance
    instance.close()


# ------------------------------------------------------------------ synthetic data

def stamp(moment=NEW):
    return moment.strftime('%Y-%m-%dT%H:%M:%S.000Z')


def line(kind, payload, moment=NEW):
    """One rollout record in the shape Codex really writes: compact JSON + LF."""
    return json.dumps({'type': kind, 'timestamp': stamp(moment), 'payload': payload},
                      ensure_ascii=False, separators=(',', ':')) + '\n'


def session_meta():
    return line('session_meta', {'id': SESSION, 'cwd': '/home/u/demo'})


def incremental_batch(text):
    """One incremental batch: a user line carrying the topic and its answer."""
    return [
        line('response_item', {'type': 'message', 'role': 'user', 'id': 'evt-user',
                               'content': [{'type': 'input_text', 'text': text}]}),
        line('response_item', {'type': 'message', 'role': 'assistant', 'id': 'evt-ai',
                               'content': [{'type': 'output_text', 'text': '已按批次归档。'}]}),
    ]


def write_capture_config(config, roots, *, since=NEW):
    (config.data_dir / 'local_codex_capture.json').write_text(json.dumps(
        {'enabled': True, 'sessions_roots': [str(roots)], 'since': stamp(since)}),
        encoding='utf-8')


def write_rollout(roots, lines):
    directory = roots / '2026' / '06' / '01'
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f'rollout-2026-06-01T00-00-00-{SESSION}.jsonl'
    path.write_text(''.join(lines), encoding='utf-8')
    return path


def append_lines(path, lines):
    with open(path, 'a', encoding='utf-8') as handle:
        handle.write(''.join(lines))
    os.utime(path, (time.time(), time.time()))


def scan(service):
    return LocalCodexCapture(service.config, service.store).scan()


def archives(service):
    return [dict(row) for row in service.store._connection().execute(
        'SELECT * FROM session_archives ORDER BY id')]


def payload(text):
    return json.dumps({'messages': [{'role': 'user', 'content': text}]}, ensure_ascii=False)


def windows_external(identity, digest):
    """The Windows client-reported identity: session head plus content digest."""
    return hashlib.sha256(json.dumps(identity).encode()).hexdigest() + ':' + digest


def cleaning_entry(service, archive_id):
    from evolvmem.knowledge_cleaning import record
    return record(service, f'archive:{archive_id}')


def permanent_delete(service, entry):
    return dispatch(service, 'POST', 'cleaning/delete', {
        'confirm_permanent': True,
        'items': [{'key': entry['key'], 'expected_revision': entry['expected_revision']}]})


def stored_units(service, task_id):
    return service.store._connection().execute(
        'SELECT text,project FROM organization_units WHERE task_id=? ORDER BY ordinal',
        (task_id,)).fetchall()


# ------------------------------------------------------------------- identity helper

def test_only_the_real_incremental_batch_shape_is_recognized():
    from evolvmem.local_codex_capture import batch_external_id
    real = batch_external_id(SESSION, 3, 9, 'f' * 64)
    assert is_incremental_batch(real)
    assert logical_identity('codex', real) == real
    # A whole Windows snapshot keeps its session head; so does every other adapter.
    snapshot = windows_external(['device', 'session'], 'a' * 64)
    assert not is_incremental_batch(snapshot)
    assert logical_identity('codex', snapshot) == snapshot.split(':')[0]
    assert logical_identity('kimi', 'session-a:part-1') == 'session-a:part-1'
    # Near misses are not silently treated as independent batches.
    assert not is_incremental_batch('f' * 32 + ':3-9')
    assert not is_incremental_batch('f' * 32 + ':3-9:' + 'A' * 16)
    assert not is_incremental_batch('f' * 64 + ':' + 'a' * 64)
    assert not is_incremental_batch('')
    assert not is_incremental_batch(None)


# ------------------------------------------------- incremental batches stay separate

def test_a_second_batch_never_replaces_a_queued_first_batch(service, config, tmp_path, monkeypatch):
    from evolvmem.auto_organization import task_view
    from evolvmem.history_organization import source_refs
    from evolvmem import history_memory
    from evolvmem.organization_arrival import discover_new, update_settings

    roots = tmp_path / 'codex-roots'
    roots.mkdir()
    write_capture_config(config, roots)
    update_settings(service, {'auto_new': True})
    path = write_rollout(roots, [session_meta()] + incremental_batch(
        '用户要求：Evo 演示项目先明确验收条件。'))
    assert scan(service)['archives'] == 1
    first = archives(service)[0]
    assert is_incremental_batch(first['external_session_id'])

    # Batch 1 is already queued when batch 2 is captured.
    queued = discover_new(service)
    assert queued['created'] == 1
    assert queued['items'][0]['source_key'] == f"archive:{first['id']}"
    first_task_id = queued['items'][0]['id']

    append_lines(path, incremental_batch('用户要求：Evo 演示项目第二批必须保留旧快照。'))
    assert scan(service)['archives'] == 1
    remaining = [row for row in archives(service) if row['id'] != first['id']]
    assert len(remaining) == 1
    second = remaining[0]
    # Same session head, different batch identity.
    assert second['external_session_id'].split(':')[0] == first['external_session_id'].split(':')[0]
    assert second['external_session_id'] != first['external_session_id']

    # The still-queued first batch stays visible and current; the arrival of
    # batch 2 does not hide it. It is absent from the manual queue only because
    # its own task already handles that key.
    from evolvmem.history_organization import _unassigned_archives
    visible = {row['id'] for row in _unassigned_archives(service)}
    assert {first['id'], second['id']} <= visible
    assert f"archive:{first['id']}" not in {ref['key'] for ref in source_refs(service)}
    assert task_view(service, first_task_id)['source_current'] is True

    tasks = run_worker(service, monkeypatch, model_for(service, projects={'evo', 'shop'}), timeout=30)
    by_key = {task['source_key']: task for task in tasks}
    first_task = by_key[f"archive:{first['id']}"]
    second_task = by_key[f"archive:{second['id']}"]
    assert first_task['error_code'] != 'classification_source_changed'
    assert first_task['status'] in ('completed', 'review'), first_task['error_code']
    assert second_task['status'] in ('completed', 'review'), second_task['error_code']
    assert stored_units(service, first_task['id'])
    assert stored_units(service, second_task['id'])

    # Both batches are readable history for the same project, not one replacing the other.
    listed = [row for row in history_memory.sessions(service, 'evo') if row['summary']]
    assert {row['id'] for row in listed} == {first['id'], second['id']}

    # A repeated scan never duplicates an already archived batch.
    assert scan(service)['archives'] == 0
    assert len(archives(service)) == 2


def test_windows_full_snapshot_versions_still_show_only_the_newest(service):
    from evolvmem.history_organization import source_refs
    from evolvmem import history_memory

    archiver = SessionArchiver(service.config, service.store)
    identity = ['windows-device', 'windows-session']
    old = archiver.archive_session('evo', 'codex', windows_external(identity, 'a' * 64),
                                   payload('旧的整份快照。'))
    new = archiver.archive_session('evo', 'codex', windows_external(identity, 'b' * 64),
                                   payload('最新的整份快照。'))
    assert old.id != new.id
    assert [row['id'] for row in history_memory.sessions(service, 'evo')] == [new.id]

    # The same holds for the unassigned queue.
    unbound = ['windows-device-2', 'windows-session-2']
    first = archiver.archive_session('', 'codex', windows_external(unbound, 'c' * 64),
                                     payload('未归属的旧快照。'))
    second = archiver.archive_session('', 'codex', windows_external(unbound, 'd' * 64),
                                      payload('未归属的最新快照。'))
    refs = {ref['key'] for ref in source_refs(service)}
    assert f'archive:{second.id}' in refs
    assert f'archive:{first.id}' not in refs


def test_both_incremental_batches_enter_the_unassigned_queue(service, config, tmp_path):
    from evolvmem.history_organization import source_refs

    roots = tmp_path / 'codex-roots'
    roots.mkdir()
    write_capture_config(config, roots)
    path = write_rollout(roots, [session_meta()] + incremental_batch(
        '用户要求：Evo 演示项目先明确验收条件。'))
    assert scan(service)['archives'] == 1
    append_lines(path, incremental_batch('用户要求：Evo 演示项目第二批必须保留旧快照。'))
    assert scan(service)['archives'] == 1
    first, second = archives(service)

    keys = {ref['key'] for ref in source_refs(service)}
    assert {f"archive:{first['id']}", f"archive:{second['id']}"} <= keys


# ------------------------------------------------------------------- delete regressions

def test_permanent_delete_of_one_incremental_batch_keeps_its_sibling(service, config, tmp_path):
    roots = tmp_path / 'codex-roots'
    roots.mkdir()
    write_capture_config(config, roots)
    path = write_rollout(roots, [session_meta()] + incremental_batch(
        '用户要求：Evo 演示项目先明确验收条件。'))
    assert scan(service)['archives'] == 1
    append_lines(path, incremental_batch('用户要求：Evo 演示项目第二批必须保留旧快照。'))
    assert scan(service)['archives'] == 1
    first, second = archives(service)

    entry = cleaning_entry(service, first['id'])
    result = permanent_delete(service, entry)
    assert result['succeeded'] == 1, result

    # Only the selected batch is purged; its sibling stays available and readable.
    assert service.store.get_session_archive(first['id'])['state'] == 'purged'
    assert service.store.get_session_archive(second['id'])['state'] == 'available'
    assert not (config.data_dir / first['payload_path']).exists()
    assert (config.data_dir / second['payload_path']).exists()
    assert service.store._connection().execute(
        'SELECT 1 FROM knowledge_cleaning_reviews WHERE source_key=?',
        (f"archive:{second['id']}",)).fetchone() is None
    assert service.store._connection().execute(
        'SELECT 1 FROM conversation_history WHERE archive_id=?', (second['id'],)).fetchone()


def test_windows_version_delete_still_purges_every_version(service, config):
    archiver = SessionArchiver(service.config, service.store)
    identity = ['windows-device-3', 'windows-session-3']
    first = archiver.archive_session('', 'codex', windows_external(identity, 'e' * 64),
                                     payload('旧的整份快照。'))
    second = archiver.archive_session('', 'codex', windows_external(identity, 'f' * 64),
                                      payload('最新的整份快照。'))
    entry = cleaning_entry(service, second.id)
    result = permanent_delete(service, entry)
    assert result['succeeded'] == 1, result
    # A whole snapshot delete keeps its original version handling: all versions go.
    assert service.store.get_session_archive(first.id)['state'] == 'purged'
    assert service.store.get_session_archive(second.id)['state'] == 'purged'
    for row in (first, second):
        assert not (config.data_dir / row.payload_path).exists()
    states = {row['source_key']: row['state'] for row in service.store._connection().execute(
        'SELECT source_key,state FROM knowledge_cleaning_reviews WHERE source_key IN (?,?)',
        (f'archive:{first.id}', f'archive:{second.id}'))}
    assert states == {f'archive:{first.id}': 'deleted', f'archive:{second.id}': 'deleted'}
