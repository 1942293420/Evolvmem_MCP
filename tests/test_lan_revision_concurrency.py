"""Revision polling must remain private, committed and independent of business work."""
import concurrent.futures
import sqlite3
import threading
import json

import pytest

from tests.test_lan_sharing import lan


def test_revision_finishes_while_business_lock_is_held(lan):
    _, adapter = lan
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        entered = threading.Event()
        def read():
            entered.set()
            return adapter.call_tool('kane', 'memory_status', {'detail': 'revision'})
        with adapter.lock:
            future = pool.submit(read)
            assert entered.wait(timeout=1)
            try:
                result = future.result(timeout=1)
            except concurrent.futures.TimeoutError:
                pytest.fail('revision polling waited for the business lock')
        assert result == {'detail': 'revision', 'authenticated_user': 'kane', 'memory_revision': '0.0'}


def test_revision_uses_committed_snapshot_and_no_resident_server(lan, monkeypatch):
    runtime, adapter = lan
    store = runtime.server_for('kane').context_service.store
    conn = store._connection()
    before = adapter.call_tool('kane', 'memory_status', {'detail': 'revision'})
    conn.execute('BEGIN IMMEDIATE')
    conn.execute('UPDATE lan_memory_revision SET revision=revision+1 WHERE id=1')
    try:
        monkeypatch.setattr(runtime, 'server_for', lambda *a, **kw: pytest.fail('resident server accessed'))
        assert adapter.call_tool('kane', 'memory_status', {'detail': 'revision'}) == before
    finally:
        conn.rollback()


def test_revision_identity_and_public_semantics(lan):
    runtime, adapter = lan
    for user, space, revision in [('jiangli', 'personal', 7), ('kane', 'personal', 12), ('jiangli', 'public', 19)]:
        conn = runtime.server_for(user, space).context_service.store._connection()
        conn.execute('UPDATE lan_memory_revision SET revision=? WHERE id=1', (revision,))
        conn.commit()
    for user, expected in [('jiangli', '7.19'), ('kane', '12.19')]:
        for space in ['personal', 'public']:
            assert adapter.call_tool(user, 'memory_status', {'detail': 'revision', 'space': space}) == {
                'detail': 'revision', 'authenticated_user': user, 'memory_revision': expected}


@pytest.mark.parametrize('user,args,owner,error', [
    ('invalid', {'detail': 'revision'}, False, 'unauthorized'),
    ('kane', {'detail': 'revision'}, True, 'owner_forbidden'),
    ('kane', {'detail': 'revision', 'user': 'jiangli'}, False, 'identity_override_forbidden'),
    ('kane', {'detail': 'revision', 'space': 'both'}, False, 'invalid_space'),
    ('kane', {'detail': 'revision', 'ref': 'public:context:1'}, False, 'invalid_arguments'),
    ('kane', {'detail': 'revision', 'ref': 'public:memory:1'}, False, 'invalid_reference'),
    ('kane', {'detail': 'revision', 'ref': 'public:context:1', 'space': 'personal'}, False, 'invalid_reference'),
    ('kane', {'detail': 'revision', 'extra': 1}, False, 'invalid_arguments'),
    ('kane', {'detail': 'revision', 'request_id': 'x'}, False, 'invalid_arguments'),
    ('kane', {'detail': 0}, False, 'invalid_arguments'),
    ('kane', {'detail': -1}, False, 'invalid_arguments'),
    ('kane', {'detail': None}, False, 'invalid_arguments'),
    ('kane', {'detail': []}, False, 'invalid_arguments'),
    ('kane', {'detail': 'Revision'}, False, 'invalid_arguments'),
])
def test_revision_retains_validation(lan, user, args, owner, error):
    _, adapter = lan
    assert adapter.call_tool(user, 'memory_status', args, owner=owner) == {'error': error}


@pytest.mark.parametrize('damage', ['missing', 'malformed', 'schema', 'alias', 'replacement'])
def test_revision_database_failure_is_safe_and_never_creates(lan, damage, tmp_path):
    runtime, adapter = lan
    db = runtime.settings.data_dir / 'users' / 'kane' / 'memory.db'
    if damage == 'schema':
        conn = runtime.server_for('kane').context_service.store._connection()
        conn.execute('DROP TABLE lan_memory_revision')
        conn.commit()
    else:
        db.rename(tmp_path / 'original.db')
        if damage == 'malformed':
            db.write_bytes(b'not a sqlite database')
        elif damage == 'alias':
            db.symlink_to(runtime.settings.owner_data_dir / 'memory.db')
        elif damage == 'replacement':
            replacement = sqlite3.connect(db)
            replacement.execute('CREATE TABLE lan_memory_revision(id INTEGER, revision INTEGER)')
            replacement.execute('INSERT INTO lan_memory_revision VALUES(1,999)')
            replacement.commit()
            replacement.close()
    assert adapter.call_tool('kane', 'memory_status', {'detail': 'revision'}) == {'error': 'remote_tool_failed'}
    if damage == 'missing':
        assert not db.exists()


@pytest.mark.parametrize('fail_query', [False, True])
def test_revision_connections_are_readonly_and_closed_on_all_paths(lan, monkeypatch, fail_query):
    runtime, adapter = lan
    import evolvmem.lan_revision as revision
    real_connect = sqlite3.connect
    handles = []
    def connect(*args, **kwargs):
        handle = real_connect(*args, **kwargs)
        handles.append(handle)
        with pytest.raises(sqlite3.OperationalError, match='readonly'):
            handle.execute('UPDATE lan_memory_revision SET revision=99 WHERE id=1')
        handle.rollback()
        return handle
    if fail_query:
        conn = runtime.server_for('jiangli', 'public').context_service.store._connection()
        conn.execute('DROP TABLE lan_memory_revision')
        conn.commit()
    monkeypatch.setattr(revision.sqlite3, 'connect', connect)
    result = adapter.call_tool('kane', 'memory_status', {'detail': 'revision'})
    assert ('error' in result) == fail_query
    assert len(handles) == 1
    for handle in handles:
        with pytest.raises(sqlite3.ProgrammingError, match='closed'):
            handle.execute('SELECT 1')


def test_revision_does_not_claim_business_queue_time(lan, monkeypatch, caplog):
    _, adapter = lan
    import evolvmem.request_trace as trace
    monkeypatch.setattr(trace, 'SLOW_MS', 0)
    with caplog.at_level('WARNING'):
        result = adapter.call_tool('kane', 'memory_status', {'detail': 'revision'})
    assert 'error' not in result
    records = [json.loads(r.message.split('request_trace ', 1)[1])
               for r in caplog.records if r.message.startswith('request_trace ')]
    assert len(records) == 1
    assert 'queue' not in records[0]['stages_ms']
    assert records[0]['stages_ms']['dispatch'] > 0
    assert records[0]['total_ms'] >= records[0]['stages_ms']['dispatch']


def test_reader_sqlite_lock_wait_is_bounded(tmp_path):
    from evolvmem.lan_revision import RevisionReader
    from tests.test_lan_sharing import settings_for
    settings = settings_for(tmp_path)
    paths = [settings.owner_data_dir / 'memory.db',
             settings.data_dir / 'users' / 'kane' / 'memory.db',
             settings.data_dir / 'public' / 'memory.db']
    for path in paths:
        path.parent.mkdir(parents=True)
        with sqlite3.connect(path) as conn:
            conn.execute('CREATE TABLE lan_memory_revision(id INTEGER PRIMARY KEY, revision INTEGER)')
            conn.execute('INSERT INTO lan_memory_revision VALUES(1,0)')
        conn.close()
    reader = RevisionReader(settings)
    conn = sqlite3.connect(paths[1])
    conn.execute('BEGIN EXCLUSIVE')
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            with pytest.raises(sqlite3.OperationalError, match='locked'):
                pool.submit(reader.read, 'kane').result(timeout=1)
    finally:
        conn.rollback()
        conn.close()


def test_reader_rejects_hardlinked_namespace_alias(tmp_path):
    import os
    from evolvmem.lan_revision import RevisionReader
    from tests.test_lan_sharing import settings_for
    settings = settings_for(tmp_path)
    owner = settings.owner_data_dir / 'memory.db'
    kane = settings.data_dir / 'users' / 'kane' / 'memory.db'
    public = settings.data_dir / 'public' / 'memory.db'
    for path in (owner, kane, public):
        path.parent.mkdir(parents=True, exist_ok=True)
    owner.touch()
    public.touch()
    os.link(owner, kane)
    with pytest.raises(RuntimeError, match='distinct'):
        RevisionReader(settings)
